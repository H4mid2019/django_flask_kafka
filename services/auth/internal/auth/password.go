package auth

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"

	"golang.org/x/crypto/argon2"
)

// Password hashing with argon2id.
//
// Not SHA-256 or bcrypt: argon2id is memory-hard, so an attacker with GPUs
// cannot parallelise it the way they can a fast hash. The parameters below are
// the low end of the RFC 9106 recommendation, chosen so login stays responsive.
//
// The encoded output carries its own parameters, which is what makes them
// changeable later: a hash written with today's cost still verifies after the
// cost is raised, because the values come from the stored string rather than
// from these constants.
const (
	argonTime    = 1
	argonMemory  = 64 * 1024 // KiB, so 64 MB
	argonThreads = 4
	argonKeyLen  = 32
	saltLen      = 16
)

var (
	ErrInvalidHash = errors.New("password hash is not in the expected format")
	ErrMismatch    = errors.New("password does not match")
)

// HashPassword returns an encoded argon2id hash, salt and parameters included.
func HashPassword(password string) (string, error) {
	salt := make([]byte, saltLen)
	if _, err := rand.Read(salt); err != nil {
		return "", fmt.Errorf("generate salt: %w", err)
	}

	key := argon2.IDKey([]byte(password), salt, argonTime, argonMemory, argonThreads, argonKeyLen)

	return fmt.Sprintf(
		"$argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
		argon2.Version, argonMemory, argonTime, argonThreads,
		base64.RawStdEncoding.EncodeToString(salt),
		base64.RawStdEncoding.EncodeToString(key),
	), nil
}

// VerifyPassword reports whether the password produces the stored hash.
func VerifyPassword(password, encoded string) error {
	parts := strings.Split(encoded, "$")
	if len(parts) != 6 || parts[1] != "argon2id" {
		return ErrInvalidHash
	}

	var version int
	if _, err := fmt.Sscanf(parts[2], "v=%d", &version); err != nil || version != argon2.Version {
		return ErrInvalidHash
	}

	var memory uint32
	var time uint32
	var threads uint8
	if _, err := fmt.Sscanf(parts[3], "m=%d,t=%d,p=%d", &memory, &time, &threads); err != nil {
		return ErrInvalidHash
	}

	salt, err := base64.RawStdEncoding.DecodeString(parts[4])
	if err != nil {
		return ErrInvalidHash
	}
	want, err := base64.RawStdEncoding.DecodeString(parts[5])
	if err != nil {
		return ErrInvalidHash
	}

	got := argon2.IDKey([]byte(password), salt, time, memory, threads, uint32(len(want)))

	// Constant time. A byte-by-byte comparison returns sooner on an early
	// mismatch, and that timing difference is measurable over enough requests.
	if subtle.ConstantTimeCompare(got, want) != 1 {
		return ErrMismatch
	}
	return nil
}
