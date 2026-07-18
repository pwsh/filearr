// Command filearr-release is the LOCAL release-signing tool for the Filearr
// agent self-updater (P5-T7). It is intentionally separate from the agent binary
// (the signing key must never ship to agents or central) and uses ONLY stdlib
// crypto/ed25519.
//
// Two subcommands:
//
//	filearr-release keygen [-dir DIR] [-force]
//	    Generate an Ed25519 signing keypair OUTSIDE the repo (default
//	    %USERPROFILE%\.filearr-signing on Windows, ~/.filearr-signing elsewhere).
//	    Refuses to overwrite an existing key. Prints the base64 PUBLIC key to pin
//	    into the agent build (-ldflags -X ...update.PublicKeyBase64=<key>).
//
//	filearr-release sign -version V [-key FILE] [-created-at RFC3339] [-out FILE] \
//	    <platform>/<arch>=<path> [<platform>/<arch>=<path> ...]
//	    Hash each built artifact (sha256 + size), emit a canonical manifest JSON
//	    and its Ed25519 signature over the canonical bytes, embedded in the
//	    manifest's ``signature`` field (and also written to <out>.sig for
//	    reference). Deterministic serialization (see update.Manifest canonicalize).
//
// The PRIVATE key lives on the operator's signing machine only; the user backs it
// up to a vault. It is NEVER committed (see the repo .gitignore guards) and never
// touches central (which is untrusted for update integrity, research §8).
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/filearr/filearr/agent/internal/update"
)

const (
	keyFileName = "filearr-release.key" // base64 of the 64-byte Ed25519 private key
	pubFileName = "filearr-release.pub" // base64 of the 32-byte public key
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "keygen":
		err = runKeygen(os.Args[2:])
	case "sign":
		err = runSign(os.Args[2:])
	case "-h", "--help", "help":
		usage()
		return
	default:
		fmt.Fprintf(os.Stderr, "filearr-release: unknown subcommand %q\n", os.Args[1])
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "filearr-release: %v\n", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `filearr-release — local Ed25519 release signing for the agent updater

usage:
  filearr-release keygen [-dir DIR] [-force]
  filearr-release sign -version V [-key FILE] [-created-at RFC3339] [-out FILE] \
      <platform>/<arch>=<path> [...]

examples:
  filearr-release keygen
  filearr-release sign -version 1.4.0 -out manifest.json \
      linux/amd64=dist/filearr-agent-linux-amd64 \
      windows/amd64=dist/filearr-agent-windows-amd64.exe \
      darwin/arm64=dist/filearr-agent-darwin-arm64
`)
}

// defaultSigningDir is %USERPROFILE%\.filearr-signing (Windows) or
// ~/.filearr-signing, OUTSIDE the repo. Overridable with -dir.
func defaultSigningDir() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return ".filearr-signing"
	}
	return filepath.Join(home, ".filearr-signing")
}

func runKeygen(args []string) error {
	fs := newFlagSet("keygen")
	dir := fs.String("dir", defaultSigningDir(), "directory to write the keypair into (outside the repo)")
	force := fs.Bool("force", false, "overwrite an existing key (DANGEROUS: invalidates every pinned agent)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	keyPath := filepath.Join(*dir, keyFileName)
	pubPath := filepath.Join(*dir, pubFileName)
	if !*force {
		if _, err := os.Stat(keyPath); err == nil {
			return fmt.Errorf("refusing to overwrite existing key %s (use -force to replace — this invalidates every pinned agent)", keyPath)
		}
	}

	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return fmt.Errorf("generate keypair: %w", err)
	}
	if err := os.MkdirAll(*dir, 0o700); err != nil {
		return fmt.Errorf("create signing dir: %w", err)
	}
	privB64 := base64.StdEncoding.EncodeToString(priv)
	pubB64 := base64.StdEncoding.EncodeToString(pub)
	if err := os.WriteFile(keyPath, []byte(privB64+"\n"), 0o600); err != nil {
		return fmt.Errorf("write private key: %w", err)
	}
	if err := os.WriteFile(pubPath, []byte(pubB64+"\n"), 0o644); err != nil {
		return fmt.Errorf("write public key: %w", err)
	}

	fmt.Printf("wrote signing keypair to %s\n", *dir)
	fmt.Printf("  private key: %s (0600 — back up to your vault, NEVER commit)\n", keyPath)
	fmt.Printf("  public key:  %s\n", pubPath)
	fmt.Println()
	fmt.Println("pin this PUBLIC key into the agent build:")
	fmt.Printf("  go build -ldflags \"-X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=%s\" ./cmd/filearr-agent\n", pubB64)
	fmt.Println()
	fmt.Printf("public key (base64): %s\n", pubB64)
	return nil
}

func runSign(args []string) error {
	fs := newFlagSet("sign")
	version := fs.String("version", "", "release version (e.g. 1.4.0) — required")
	keyPath := fs.String("key", filepath.Join(defaultSigningDir(), keyFileName), "path to the private signing key")
	createdAt := fs.String("created-at", "", "manifest created_at (RFC3339 UTC); default now")
	out := fs.String("out", "manifest.json", "manifest output path")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(*version) == "" {
		return fmt.Errorf("-version is required")
	}
	specs := fs.Args()
	if len(specs) == 0 {
		return fmt.Errorf("at least one <platform>/<arch>=<path> artifact is required")
	}

	priv, err := loadPrivateKey(*keyPath)
	if err != nil {
		return err
	}

	ts := *createdAt
	if ts == "" {
		ts = time.Now().UTC().Format(time.RFC3339)
	}

	m := update.Manifest{Version: *version, CreatedAt: ts}
	for _, spec := range specs {
		art, err := artifactFromSpec(spec)
		if err != nil {
			return err
		}
		m.Artifacts = append(m.Artifacts, art)
	}

	sig, err := update.Sign(m, priv)
	if err != nil {
		return err
	}
	m.Signature = sig

	doc, err := update.Marshal(m)
	if err != nil {
		return err
	}
	if err := os.WriteFile(*out, append(doc, '\n'), 0o644); err != nil {
		return fmt.Errorf("write manifest: %w", err)
	}
	if err := os.WriteFile(*out+".sig", []byte(sig+"\n"), 0o644); err != nil {
		return fmt.Errorf("write signature: %w", err)
	}

	fmt.Printf("signed manifest %s (version %s, %d artifact(s))\n", *out, m.Version, len(m.Artifacts))
	for _, a := range m.Artifacts {
		fmt.Printf("  %s/%s  %s  %d bytes  sha256=%s\n", a.Platform, a.Arch, a.URL, a.Size, a.SHA256)
	}
	fmt.Printf("signature (base64): %s\n", sig)
	return nil
}

// artifactFromSpec parses "<platform>/<arch>=<path>" and hashes the file.
func artifactFromSpec(spec string) (update.Artifact, error) {
	key, path, ok := strings.Cut(spec, "=")
	if !ok {
		return update.Artifact{}, fmt.Errorf("bad artifact spec %q (want <platform>/<arch>=<path>)", spec)
	}
	platform, arch, ok := strings.Cut(key, "/")
	if !ok || platform == "" || arch == "" {
		return update.Artifact{}, fmt.Errorf("bad artifact key %q (want <platform>/<arch>)", key)
	}
	sum, size, err := sha256File(path)
	if err != nil {
		return update.Artifact{}, err
	}
	return update.Artifact{
		Platform: platform,
		Arch:     arch,
		SHA256:   sum,
		Size:     size,
		URL:      filepath.Base(path), // artifact FILENAME, never a path
	}, nil
}

func loadPrivateKey(path string) (ed25519.PrivateKey, error) {
	buf, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read signing key %s (run `filearr-release keygen` first): %w", path, err)
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(buf)))
	if err != nil {
		return nil, fmt.Errorf("decode signing key: %w", err)
	}
	if len(raw) != ed25519.PrivateKeySize {
		return nil, fmt.Errorf("signing key wrong size: got %d, want %d", len(raw), ed25519.PrivateKeySize)
	}
	return ed25519.PrivateKey(raw), nil
}
