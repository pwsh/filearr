package update

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"testing"
)

func testKeypair(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("keygen: %v", err)
	}
	return pub, priv
}

func sampleManifest() Manifest {
	return Manifest{
		Version:   "1.4.0",
		CreatedAt: "2026-07-17T12:00:00Z",
		Artifacts: []Artifact{
			{Platform: "linux", Arch: "amd64", SHA256: "aa", Size: 10, URL: "filearr-agent-linux-amd64"},
			{Platform: "windows", Arch: "amd64", SHA256: "bb", Size: 11, URL: "filearr-agent-windows-amd64.exe"},
			{Platform: "macos", Arch: "arm64", SHA256: "cc", Size: 12, URL: "filearr-agent-macos-arm64"},
		},
	}
}

func TestSignVerifyRoundTrip(t *testing.T) {
	pub, priv := testKeypair(t)
	m := sampleManifest()
	sig, err := Sign(m, priv)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	m.Signature = sig
	if err := Verify(m, pub); err != nil {
		t.Fatalf("verify: %v", err)
	}
}

func TestVerifyRejectsTamper(t *testing.T) {
	pub, priv := testKeypair(t)
	m := sampleManifest()
	sig, _ := Sign(m, priv)
	m.Signature = sig

	// Mutate a signed field: verification must now fail.
	tampered := m
	tampered.Artifacts = append([]Artifact(nil), m.Artifacts...)
	tampered.Artifacts[0].SHA256 = "deadbeef"
	if err := Verify(tampered, pub); err != ErrBadSignature {
		t.Fatalf("tampered manifest verified: got %v, want ErrBadSignature", err)
	}

	// A different key must reject a validly-signed manifest.
	otherPub, _ := testKeypair(t)
	if err := Verify(m, otherPub); err != ErrBadSignature {
		t.Fatalf("wrong-key verify: got %v, want ErrBadSignature", err)
	}
}

func TestVerifyRejectsUnsignedAndNoKey(t *testing.T) {
	pub, _ := testKeypair(t)
	m := sampleManifest()
	if err := Verify(m, pub); err != ErrUnsigned {
		t.Fatalf("unsigned: got %v, want ErrUnsigned", err)
	}
	m.Signature = "not-base64!!!"
	if err := Verify(m, nil); err != ErrNoPinnedKey {
		t.Fatalf("no key: got %v, want ErrNoPinnedKey", err)
	}
}

func TestCanonicalIsOrderIndependent(t *testing.T) {
	m1 := sampleManifest()
	m2 := sampleManifest()
	// Reverse the artifact order; canonical bytes (and thus the signature) must
	// be identical because canonicalization sorts.
	m2.Artifacts[0], m2.Artifacts[2] = m2.Artifacts[2], m2.Artifacts[0]
	b1, _ := m1.canonicalBytes()
	b2, _ := m2.canonicalBytes()
	if string(b1) != string(b2) {
		t.Fatalf("canonical bytes differ by artifact order:\n%s\n%s", b1, b2)
	}
	// A signature made over one order verifies against the other.
	pub, priv := testKeypair(t)
	sig, _ := Sign(m1, priv)
	m2.Signature = sig
	if err := Verify(m2, pub); err != nil {
		t.Fatalf("reordered manifest failed verify: %v", err)
	}
}

func TestVerifyRobustToJSONBRoundTrip(t *testing.T) {
	// Simulate central storing the manifest as JSONB (re-serializing it): parse
	// from a re-marshaled body and confirm the signature still verifies (the
	// canonical bytes are recomputed from parsed fields, not the stored layout).
	pub, priv := testKeypair(t)
	m := sampleManifest()
	sig, _ := Sign(m, priv)
	m.Signature = sig
	doc, err := Marshal(m)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	parsed, err := ParseManifest(doc)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if err := Verify(parsed, pub); err != nil {
		t.Fatalf("round-tripped manifest failed verify: %v", err)
	}
}

func TestDecodePublicKey(t *testing.T) {
	pub, _ := testKeypair(t)
	b64 := base64.StdEncoding.EncodeToString(pub)
	got, err := DecodePublicKey(" " + b64 + "\n")
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !got.Equal(pub) {
		t.Fatal("decoded key mismatch")
	}
	if _, err := DecodePublicKey(""); err != ErrNoPinnedKey {
		t.Fatalf("empty: got %v, want ErrNoPinnedKey", err)
	}
	if _, err := DecodePublicKey("###"); err == nil {
		t.Fatal("malformed key decoded without error")
	}
}
