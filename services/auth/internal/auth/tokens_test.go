package auth

import (
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"math/big"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/google/uuid"
)

func testIssuer(t *testing.T, ttl time.Duration) *Issuer {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	return NewIssuer(key, "http://auth.test", "test-audience", ttl)
}

func TestIssuedTokenVerifies(t *testing.T) {
	issuer := testIssuer(t, time.Hour)
	userID := uuid.New()

	token, tokenID, err := issuer.IssueAccess(userID, "person@example.com", time.Now())
	if err != nil {
		t.Fatalf("issue: %v", err)
	}

	claims, err := issuer.Verify(token)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	if claims.Subject != userID.String() {
		t.Errorf("subject = %q, want %q", claims.Subject, userID)
	}
	if claims.Email != "person@example.com" {
		t.Errorf("email = %q", claims.Email)
	}
	if claims.ID != tokenID {
		t.Errorf("jti = %q, want %q, so revocation could not target this token", claims.ID, tokenID)
	}
}

func TestExpiredTokenIsRejected(t *testing.T) {
	issuer := testIssuer(t, time.Minute)

	// Issued an hour ago with a one minute life.
	token, _, err := issuer.IssueAccess(uuid.New(), "a@b.c", time.Now().Add(-time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := issuer.Verify(token); err == nil {
		t.Error("an expired token was accepted")
	}
}

func TestTamperedTokenIsRejected(t *testing.T) {
	issuer := testIssuer(t, time.Hour)
	token, _, err := issuer.IssueAccess(uuid.New(), "a@b.c", time.Now())
	if err != nil {
		t.Fatal(err)
	}

	if _, err := issuer.Verify(token[:len(token)-4] + "AAAA"); err == nil {
		t.Error("a token with a broken signature was accepted")
	}
}

func TestTokenFromAnotherKeyIsRejected(t *testing.T) {
	issuer := testIssuer(t, time.Hour)
	other := testIssuer(t, time.Hour)

	token, _, err := other.IssueAccess(uuid.New(), "a@b.c", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := issuer.Verify(token); err == nil {
		t.Error("a token signed by a different key was accepted")
	}
}

// TestUnsignedTokenIsRejected covers the alg=none forgery.
//
// A verifier that trusts the header's algorithm will accept a token with no
// signature at all. Verify pins RS256, so this must fail.
func TestUnsignedTokenIsRejected(t *testing.T) {
	issuer := testIssuer(t, time.Hour)

	claims := Claims{RegisteredClaims: jwt.RegisteredClaims{
		Subject:   uuid.NewString(),
		Issuer:    "http://auth.test",
		Audience:  jwt.ClaimStrings{"test-audience"},
		ExpiresAt: jwt.NewNumericDate(time.Now().Add(time.Hour)),
	}}
	forged, err := jwt.NewWithClaims(jwt.SigningMethodNone, claims).
		SignedString(jwt.UnsafeAllowNoneSignatureType)
	if err != nil {
		t.Fatalf("build unsigned token: %v", err)
	}

	if _, err := issuer.Verify(forged); err == nil {
		t.Error("an unsigned token was accepted")
	}
}

func TestWrongAudienceIsRejected(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	mine := NewIssuer(key, "http://auth.test", "test-audience", time.Hour)
	theirs := NewIssuer(key, "http://auth.test", "a-different-service", time.Hour)

	// Same signing key, different intended recipient. A token minted for
	// another service must not be usable here.
	token, _, err := theirs.IssueAccess(uuid.New(), "a@b.c", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := mine.Verify(token); err == nil {
		t.Error("a token issued for another audience was accepted")
	}
}

// TestJWKSMatchesTheSigningKey is what makes local verification possible.
//
// If the published modulus and exponent do not reconstruct the real public key,
// every other service silently rejects every token.
func TestJWKSMatchesTheSigningKey(t *testing.T) {
	issuer := testIssuer(t, time.Hour)
	jwks := issuer.PublicJWKS()

	if len(jwks.Keys) != 1 {
		t.Fatalf("published %d keys, want 1", len(jwks.Keys))
	}
	key := jwks.Keys[0]
	if key.Kty != "RSA" || key.Alg != "RS256" || key.Use != "sig" {
		t.Errorf("unexpected jwk metadata: %+v", key)
	}

	nBytes, err := base64.RawURLEncoding.DecodeString(key.N)
	if err != nil {
		t.Fatalf("decode modulus: %v", err)
	}
	eBytes, err := base64.RawURLEncoding.DecodeString(key.E)
	if err != nil {
		t.Fatalf("decode exponent: %v", err)
	}

	rebuilt := &rsa.PublicKey{
		N: new(big.Int).SetBytes(nBytes),
		E: int(new(big.Int).SetBytes(eBytes).Int64()),
	}

	if rebuilt.N.Cmp(issuer.privateKey.PublicKey.N) != 0 {
		t.Error("published modulus does not match the signing key")
	}
	if rebuilt.E != issuer.privateKey.PublicKey.E {
		t.Errorf("published exponent %d, want %d", rebuilt.E, issuer.privateKey.PublicKey.E)
	}

	// The reconstructed key must actually verify a real token, which is what a
	// remote service will do with it.
	token, _, err := issuer.IssueAccess(uuid.New(), "a@b.c", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := jwt.Parse(token, func(*jwt.Token) (any, error) { return rebuilt, nil })
	if err != nil || !parsed.Valid {
		t.Errorf("token did not verify against the published key: %v", err)
	}
}

func TestKeyIDIsInTheHeaderAndStableForAKey(t *testing.T) {
	issuer := testIssuer(t, time.Hour)
	token, _, err := issuer.IssueAccess(uuid.New(), "a@b.c", time.Now())
	if err != nil {
		t.Fatal(err)
	}

	parsed, _, err := jwt.NewParser().ParseUnverified(token, &Claims{})
	if err != nil {
		t.Fatal(err)
	}
	kid, ok := parsed.Header["kid"].(string)
	if !ok || kid == "" {
		t.Fatal("no kid in the token header, so a verifier cannot pick a key during rotation")
	}
	if kid != issuer.PublicJWKS().Keys[0].Kid {
		t.Errorf("header kid %q does not match the published kid", kid)
	}
}

func TestRefreshTokensAreRandomAndStoredHashed(t *testing.T) {
	first, firstHash, err := NewRefreshToken()
	if err != nil {
		t.Fatal(err)
	}
	second, _, err := NewRefreshToken()
	if err != nil {
		t.Fatal(err)
	}

	if first == second {
		t.Error("two refresh tokens came out identical")
	}
	if strings.Contains(firstHash, first) {
		t.Error("the stored hash contains the token itself")
	}
	if HashRefreshToken(first) != firstHash {
		t.Error("hashing is not deterministic, so lookup would never match")
	}
	if len(first) < 40 {
		t.Errorf("token is only %d characters, too little entropy", len(first))
	}
}
