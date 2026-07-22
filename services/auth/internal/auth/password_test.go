package auth

import (
	"strings"
	"testing"
)

func TestPasswordRoundTrip(t *testing.T) {
	hash, err := HashPassword("correct horse battery staple")
	if err != nil {
		t.Fatalf("hash: %v", err)
	}
	if err := VerifyPassword("correct horse battery staple", hash); err != nil {
		t.Errorf("the right password did not verify: %v", err)
	}
	if err := VerifyPassword("wrong password entirely", hash); err == nil {
		t.Error("the wrong password verified")
	}
}

func TestHashIsSaltedSoIdenticalPasswordsDiffer(t *testing.T) {
	first, err := HashPassword("same password")
	if err != nil {
		t.Fatal(err)
	}
	second, err := HashPassword("same password")
	if err != nil {
		t.Fatal(err)
	}

	// Equal hashes would mean no salt, which makes the whole table crackable
	// with one precomputed dictionary.
	if first == second {
		t.Error("two hashes of the same password are identical, so it is unsalted")
	}
	if err := VerifyPassword("same password", second); err != nil {
		t.Errorf("second hash does not verify: %v", err)
	}
}

func TestHashDoesNotContainThePassword(t *testing.T) {
	hash, err := HashPassword("hunter2-is-my-password")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(hash, "hunter2") {
		t.Error("the password appears in its own hash")
	}
}

func TestHashCarriesItsParameters(t *testing.T) {
	// The encoded form records the cost, which is what allows raising it later
	// without invalidating hashes written under the old settings.
	hash, err := HashPassword("whatever")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"$argon2id$", "m=", "t=", "p="} {
		if !strings.Contains(hash, want) {
			t.Errorf("hash %q is missing %q", hash, want)
		}
	}
}

func TestMalformedHashIsRejectedNotAccepted(t *testing.T) {
	// The dangerous failure is a parse error being treated as a match.
	for _, bad := range []string{
		"",
		"not-a-hash",
		"$argon2id$broken",
		"$bcrypt$v=19$m=65536,t=1,p=4$c2FsdA$aGFzaA",
	} {
		if err := VerifyPassword("anything", bad); err == nil {
			t.Errorf("malformed hash %q was accepted", bad)
		}
	}
}
