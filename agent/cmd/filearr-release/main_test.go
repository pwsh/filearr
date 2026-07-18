package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/filearr/filearr/agent/internal/update"
)

// TestKeygenSignVerify exercises the full local release pipeline: generate a
// keypair, sign a dummy artifact, and confirm the emitted manifest verifies
// against the printed public key (the exact check the agent performs).
func TestKeygenSignVerify(t *testing.T) {
	dir := t.TempDir()
	if err := runKeygen([]string{"-dir", dir}); err != nil {
		t.Fatalf("keygen: %v", err)
	}
	// Refuses to overwrite without -force.
	if err := runKeygen([]string{"-dir", dir}); err == nil {
		t.Fatal("keygen overwrote an existing key without -force")
	}

	pubB64Raw, err := os.ReadFile(filepath.Join(dir, pubFileName))
	if err != nil {
		t.Fatalf("read pub: %v", err)
	}
	pub, err := update.DecodePublicKey(strings.TrimSpace(string(pubB64Raw)))
	if err != nil {
		t.Fatalf("decode pub: %v", err)
	}

	artPath := filepath.Join(dir, "filearr-agent-linux-amd64")
	if err := os.WriteFile(artPath, []byte("DUMMY-BINARY-PAYLOAD"), 0o755); err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(dir, "manifest.json")
	if err := runSign([]string{
		"-version", "2.0.0",
		"-key", filepath.Join(dir, keyFileName),
		"-out", out,
		"linux/amd64=" + artPath,
	}); err != nil {
		t.Fatalf("sign: %v", err)
	}

	doc, err := os.ReadFile(out)
	if err != nil {
		t.Fatalf("read manifest: %v", err)
	}
	m, err := update.ParseManifest(doc)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if err := update.Verify(m, pub); err != nil {
		t.Fatalf("verify signed manifest: %v", err)
	}
	if len(m.Artifacts) != 1 || m.Artifacts[0].URL != "filearr-agent-linux-amd64" {
		t.Fatalf("wrong artifact: %+v", m.Artifacts)
	}
	// The .sig sidecar exists and matches the embedded signature.
	sig, err := os.ReadFile(out + ".sig")
	if err != nil {
		t.Fatalf("read sig: %v", err)
	}
	if strings.TrimSpace(string(sig)) != m.Signature {
		t.Fatal("sidecar signature != embedded signature")
	}
}
