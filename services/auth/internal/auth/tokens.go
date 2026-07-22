// Package auth issues and describes tokens.
//
// The design decision that matters: this service signs access tokens with an
// RSA private key and publishes the public half at /.well-known/jwks.json.
// Other services fetch that once, cache it, and verify signatures themselves.
//
// The alternative, asking this service to validate every request, would put a
// network hop on the hot path of every other service and make auth a hard
// dependency of all of them. This service could then be down for a deploy and
// take the whole system with it. Local verification means it is only needed to
// log in.
package auth

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/google/uuid"
)

const (
	// Short, because an access token cannot be withdrawn. The revocation topic
	// closes the gap faster, but a short lifetime is what bounds the damage if
	// a consumer misses the message.
	DefaultAccessTTL  = 15 * time.Minute
	DefaultRefreshTTL = 30 * 24 * time.Hour
)

// Claims is what other services read out of a verified token.
type Claims struct {
	jwt.RegisteredClaims
	Email string `json:"email"`
	// TokenID is echoed in the revocation event, so a service can refuse one
	// specific token rather than every token belonging to that user.
	TokenID string `json:"jti_ref,omitempty"`
}

type Issuer struct {
	privateKey *rsa.PrivateKey
	keyID      string
	issuer     string
	audience   string
	accessTTL  time.Duration
}

func NewIssuer(key *rsa.PrivateKey, issuerURL, audience string, accessTTL time.Duration) *Issuer {
	if accessTTL <= 0 {
		accessTTL = DefaultAccessTTL
	}
	return &Issuer{
		privateKey: key,
		keyID:      KeyID(&key.PublicKey),
		issuer:     issuerURL,
		audience:   audience,
		accessTTL:  accessTTL,
	}
}

// KeyID derives a stable identifier from the public key itself.
//
// Deriving it rather than hard-coding a name means the kid changes when the key
// changes, which is what lets a verifier hold several keys during a rotation and
// still pick the right one.
func KeyID(pub *rsa.PublicKey) string {
	sum := sha256.Sum256(pub.N.Bytes())
	return hex.EncodeToString(sum[:8])
}

// IssueAccess signs a short-lived access token for a user.
func (i *Issuer) IssueAccess(userID uuid.UUID, email string, now time.Time) (string, string, error) {
	tokenID := uuid.NewString()

	claims := Claims{
		RegisteredClaims: jwt.RegisteredClaims{
			Subject:   userID.String(),
			Issuer:    i.issuer,
			Audience:  jwt.ClaimStrings{i.audience},
			ID:        tokenID,
			IssuedAt:  jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(now.Add(i.accessTTL)),
			NotBefore: jwt.NewNumericDate(now),
		},
		Email:   email,
		TokenID: tokenID,
	}

	token := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	token.Header["kid"] = i.keyID

	signed, err := token.SignedString(i.privateKey)
	if err != nil {
		return "", "", fmt.Errorf("sign access token: %w", err)
	}
	return signed, tokenID, nil
}

// Verify checks a token with this issuer's own key.
//
// Other services do not call this; they verify locally against the JWKS. It
// exists so the refresh and revoke endpoints can read a token they were handed,
// and so the tests check the same path a verifier would take.
func (i *Issuer) Verify(token string) (*Claims, error) {
	parsed, err := jwt.ParseWithClaims(
		token,
		&Claims{},
		func(t *jwt.Token) (any, error) {
			// Pin the algorithm. Accepting whatever the header claims is how
			// "alg": "none" and HMAC-with-the-public-key forgeries get in.
			if _, ok := t.Method.(*jwt.SigningMethodRSA); !ok {
				return nil, fmt.Errorf("unexpected signing method %v", t.Header["alg"])
			}
			return &i.privateKey.PublicKey, nil
		},
		jwt.WithIssuer(i.issuer),
		jwt.WithAudience(i.audience),
		jwt.WithExpirationRequired(),
	)
	if err != nil {
		return nil, fmt.Errorf("verify: %w", err)
	}

	claims, ok := parsed.Claims.(*Claims)
	if !ok || !parsed.Valid {
		return nil, fmt.Errorf("token is not valid")
	}
	return claims, nil
}

// JWKS is the public key document other services fetch.
type JWKS struct {
	Keys []JWK `json:"keys"`
}

type JWK struct {
	Kty string `json:"kty"`
	Use string `json:"use"`
	Alg string `json:"alg"`
	Kid string `json:"kid"`
	N   string `json:"n"`
	E   string `json:"e"`
}

// PublicJWKS renders the public half of the signing key.
func (i *Issuer) PublicJWKS() JWKS {
	pub := &i.privateKey.PublicKey
	return JWKS{Keys: []JWK{{
		Kty: "RSA",
		Use: "sig",
		Alg: "RS256",
		Kid: i.keyID,
		N:   base64.RawURLEncoding.EncodeToString(pub.N.Bytes()),
		E:   base64.RawURLEncoding.EncodeToString(bigEndian(pub.E)),
	}}}
}

func bigEndian(value int) []byte {
	out := []byte{byte(value >> 16), byte(value >> 8), byte(value)}
	for len(out) > 1 && out[0] == 0 {
		out = out[1:]
	}
	return out
}

// NewRefreshToken returns the token to hand to the client and the hash to store.
//
// Only the hash is persisted. A refresh token is a credential, and a database
// dump should not contain working ones.
func NewRefreshToken() (token string, hash string, err error) {
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return "", "", fmt.Errorf("generate refresh token: %w", err)
	}
	token = base64.RawURLEncoding.EncodeToString(raw)
	return token, HashRefreshToken(token), nil
}

// HashRefreshToken is SHA-256, not a password hash.
//
// Deliberate: the token is 256 bits of randomness from crypto/rand, so there is
// no dictionary to attack and no need for a slow KDF. Passwords get argon2id
// because humans choose them.
func HashRefreshToken(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}
