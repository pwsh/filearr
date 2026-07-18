package enroll

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"math/big"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

// TestCertStoreRoundTrip persists a synthetic identity and reloads it,
// asserting key/cert/roots/state all survive the atomic writes.
func TestCertStoreRoundTrip(t *testing.T) {
	dir := t.TempDir()
	store := NewCertStore(dir)

	key, leaf := selfSigned(t, "leaf-cn")
	_, root := selfSigned(t, "root-cn")

	id := Identity{
		Key:   key,
		Leaf:  leaf,
		Chain: []*x509.Certificate{root},
		Roots: []*x509.Certificate{root},
		State: State{AgentID: "agent-1", CentralURL: "https://central", RolloutGroup: "canary", CAURL: "https://ca", CARootSHA256: "deadbeef"},
	}
	if err := store.SaveIdentity(id); err != nil {
		t.Fatalf("save: %v", err)
	}

	loaded, err := store.Load()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if !loaded.Leaf.Equal(leaf) {
		t.Fatalf("leaf mismatch after round-trip")
	}
	if len(loaded.Chain) != 1 || !loaded.Chain[0].Equal(root) {
		t.Fatalf("chain mismatch: %+v", loaded.Chain)
	}
	if len(loaded.Roots) != 1 || !loaded.Roots[0].Equal(root) {
		t.Fatalf("roots mismatch")
	}
	if loaded.State != id.State {
		t.Fatalf("state mismatch: %+v != %+v", loaded.State, id.State)
	}

	// The private key round-trips to the same public key.
	loadedKey, ok := loaded.Key.(*ecdsa.PrivateKey)
	if !ok || !loadedKey.PublicKey.Equal(&key.PublicKey) {
		t.Fatalf("key mismatch after round-trip")
	}

	// TLSCertificate + RootPool build without error.
	if _, err := store.TLSCertificate(); err != nil {
		t.Fatalf("tls cert: %v", err)
	}
	if pool, err := store.RootPool(); err != nil || pool == nil {
		t.Fatalf("root pool: %v", err)
	}

	// Key file permissions are 0600 on POSIX (best-effort; Windows uses ACLs).
	if runtime.GOOS != "windows" {
		info, err := os.Stat(filepath.Join(dir, keyFileName))
		if err != nil {
			t.Fatalf("stat key: %v", err)
		}
		if perm := info.Mode().Perm(); perm != 0o600 {
			t.Fatalf("key perm = %o, want 600", perm)
		}
	}
}

// TestCertStoreRenewalReplacesCertKeepsKey verifies SaveCertificate rewrites
// only the leaf+chain, leaving the key intact (the renewal path).
func TestCertStoreRenewalReplacesCertKeepsKey(t *testing.T) {
	dir := t.TempDir()
	store := NewCertStore(dir)
	key, leaf := selfSigned(t, "v1")
	_, root := selfSigned(t, "root")
	if err := store.SaveIdentity(Identity{Key: key, Leaf: leaf, Roots: []*x509.Certificate{root}, State: State{AgentID: "a"}}); err != nil {
		t.Fatalf("save: %v", err)
	}
	keyBefore, err := os.ReadFile(filepath.Join(dir, keyFileName))
	if err != nil {
		t.Fatalf("read key: %v", err)
	}

	_, leaf2 := selfSigned(t, "v2")
	if err := store.SaveCertificate(leaf2, nil); err != nil {
		t.Fatalf("save cert: %v", err)
	}
	keyAfter, err := os.ReadFile(filepath.Join(dir, keyFileName))
	if err != nil {
		t.Fatalf("read key: %v", err)
	}
	if string(keyBefore) != string(keyAfter) {
		t.Fatalf("renewal must not touch the private key")
	}
	loaded, err := store.Load()
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if !loaded.Leaf.Equal(leaf2) {
		t.Fatalf("leaf was not replaced by renewal")
	}
}

// TestLoadMissingIdentity errors cleanly when nothing is enrolled.
func TestLoadMissingIdentity(t *testing.T) {
	store := NewCertStore(t.TempDir())
	if _, err := store.Load(); err == nil {
		t.Fatalf("expected an error loading an empty data dir")
	}
}

// selfSigned returns a throwaway P-256 key + self-signed cert for store tests.
func selfSigned(t *testing.T, cn string) (*ecdsa.PrivateKey, *x509.Certificate) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("gen key: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: cn},
		DNSNames:     []string{cn},
		NotBefore:    time.Now().Add(-time.Minute),
		NotAfter:     time.Now().Add(time.Hour),
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create cert: %v", err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("parse cert: %v", err)
	}
	return key, cert
}
