package update

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeCentral serves a signed manifest + artifact bytes for one agent.
type fakeCentral struct {
	t         *testing.T
	manifest  Manifest // served as-is (200); if Version=="" -> 204
	artifact  []byte
	artifactN string // filename served under /releases/{version}/artifacts/{name}
	lastCur   string // last ?current= reported by the agent
}

func (f *fakeCentral) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/update-manifest"):
			f.lastCur = r.URL.Query().Get("current")
			if f.manifest.Version == "" {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			doc, _ := Marshal(f.manifest)
			_, _ = w.Write(doc)
		case strings.Contains(r.URL.Path, "/artifacts/"):
			if !strings.HasSuffix(r.URL.Path, f.artifactN) {
				http.Error(w, "no such artifact", http.StatusNotFound)
				return
			}
			_, _ = w.Write(f.artifact)
		default:
			http.Error(w, "not found", http.StatusNotFound)
		}
	})
}

func sha256Hex(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// buildUpdater wires an Updater against a fake central with stubbed reExec/exit
// so an "apply" never actually forks the test binary.
func buildUpdater(t *testing.T, srv *httptest.Server, pub ed25519.PublicKey, dataDir, exePath, current, plat, arch string, reExec func(string, []string) error) *Updater {
	t.Helper()
	exit := func(int) {} // no-op; ApplyUpdate returns after this in tests
	return New(Config{
		BaseURL:        srv.URL,
		AgentID:        "00000000-0000-0000-0000-000000000001",
		DataDir:        dataDir,
		CurrentVersion: current,
		PublicKey:      pub,
		ExePath:        exePath,
		Platform:       plat,
		Arch:           arch,
		reExec:         reExec,
		exit:           exit,
	})
}

func signedManifest(t *testing.T, priv ed25519.PrivateKey, version, filename string, art []byte, plat, arch string) Manifest {
	t.Helper()
	m := Manifest{
		Version:   version,
		CreatedAt: "2026-07-17T12:00:00Z",
		Artifacts: []Artifact{{Platform: plat, Arch: arch, SHA256: sha256Hex(art), Size: int64(len(art)), URL: filename}},
	}
	sig, err := Sign(m, priv)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	m.Signature = sig
	return m
}

func TestCheckForUpdateOffered(t *testing.T) {
	pub, priv := testKeypair(t)
	art := []byte("NEW-BINARY-BYTES")
	fc := &fakeCentral{t: t, manifest: signedManifest(t, priv, "1.5.0", "agent-linux-amd64", art, "linux", "amd64"), artifact: art, artifactN: "agent-linux-amd64"}
	srv := httptest.NewServer(fc.handler())
	defer srv.Close()

	u := buildUpdater(t, srv, pub, t.TempDir(), "", "1.4.0", "linux", "amd64", nil)
	m, a, ok, err := u.CheckForUpdate(context.Background())
	if err != nil || !ok {
		t.Fatalf("expected an offered update: ok=%v err=%v", ok, err)
	}
	if m.Version != "1.5.0" || a.URL != "agent-linux-amd64" {
		t.Fatalf("wrong artifact: %+v", a)
	}
	if fc.lastCur != "1.4.0" {
		t.Fatalf("agent did not report current version: %q", fc.lastCur)
	}
}

func TestCheckForUpdateRefusesBadSignature(t *testing.T) {
	pub, priv := testKeypair(t)
	art := []byte("x")
	m := signedManifest(t, priv, "1.5.0", "a", art, "linux", "amd64")
	m.Artifacts[0].SHA256 = "tampered" // invalidate the signature
	fc := &fakeCentral{t: t, manifest: m, artifact: art, artifactN: "a"}
	srv := httptest.NewServer(fc.handler())
	defer srv.Close()

	u := buildUpdater(t, srv, pub, t.TempDir(), "", "1.4.0", "linux", "amd64", nil)
	if _, _, ok, err := u.CheckForUpdate(context.Background()); ok || err == nil {
		t.Fatalf("bad signature was not refused: ok=%v err=%v", ok, err)
	}
}

func TestCheckForUpdateUpToDate(t *testing.T) {
	pub, priv := testKeypair(t)
	// Signed but NOT newer than current.
	art := []byte("x")
	fc := &fakeCentral{t: t, manifest: signedManifest(t, priv, "1.4.0", "a", art, "linux", "amd64"), artifact: art, artifactN: "a"}
	srv := httptest.NewServer(fc.handler())
	defer srv.Close()
	u := buildUpdater(t, srv, pub, t.TempDir(), "", "1.4.0", "linux", "amd64", nil)
	if _, _, ok, err := u.CheckForUpdate(context.Background()); ok || err != nil {
		t.Fatalf("same version should not update: ok=%v err=%v", ok, err)
	}

	// 204 path.
	fc.manifest = Manifest{}
	if _, _, ok, err := u.CheckForUpdate(context.Background()); ok || err != nil {
		t.Fatalf("204 should be up-to-date: ok=%v err=%v", ok, err)
	}
}

func TestApplyUpdateSwapsAndWritesState(t *testing.T) {
	pub, priv := testKeypair(t)
	art := []byte("NEW-BINARY-BYTES")
	fc := &fakeCentral{t: t, manifest: signedManifest(t, priv, "1.5.0", "agent-linux-amd64", art, "linux", "amd64"), artifact: art, artifactN: "agent-linux-amd64"}
	srv := httptest.NewServer(fc.handler())
	defer srv.Close()

	dir := t.TempDir()
	exe := filepath.Join(dir, "filearr-agent")
	writeFile(t, exe, "OLD-BINARY")

	var reExeced string
	reExec := func(path string, _ []string) error { reExeced = path; return nil }
	u := buildUpdater(t, srv, pub, dir, exe, "1.4.0", "linux", "amd64", reExec)

	m, a, ok, err := u.CheckForUpdate(context.Background())
	if err != nil || !ok {
		t.Fatalf("check: ok=%v err=%v", ok, err)
	}
	if err := u.ApplyUpdate(context.Background(), m, a); err != nil {
		t.Fatalf("apply: %v", err)
	}
	if got := readFile(t, exe); got != "NEW-BINARY-BYTES" {
		t.Fatalf("current binary=%q, want new bytes", got)
	}
	if got := readFile(t, PreviousBinaryPath(exe)); got != "OLD-BINARY" {
		t.Fatalf("previous binary=%q, want OLD-BINARY", got)
	}
	if reExeced != exe {
		t.Fatalf("re-exec path=%q, want %q", reExeced, exe)
	}
	st, err := LoadState(dir)
	if err != nil || st == nil {
		t.Fatalf("state after apply: st=%v err=%v", st, err)
	}
	if st.NewVersion != "1.5.0" || st.Attempts != 0 {
		t.Fatalf("bad state: %+v", st)
	}
}

func TestDownloadRefusesSha256Mismatch(t *testing.T) {
	pub, priv := testKeypair(t)
	art := []byte("REAL-BYTES")
	m := signedManifest(t, priv, "1.5.0", "a", art, "linux", "amd64")
	// Serve DIFFERENT bytes than the (correctly signed) manifest declares.
	fc := &fakeCentral{t: t, manifest: m, artifact: []byte("CORRUPTED"), artifactN: "a"}
	srv := httptest.NewServer(fc.handler())
	defer srv.Close()

	dir := t.TempDir()
	exe := filepath.Join(dir, "filearr-agent")
	writeFile(t, exe, "OLD")
	u := buildUpdater(t, srv, pub, dir, exe, "1.4.0", "linux", "amd64", func(string, []string) error { return nil })

	mm, a, ok, err := u.CheckForUpdate(context.Background())
	if err != nil || !ok {
		t.Fatalf("check: ok=%v err=%v", ok, err)
	}
	if err := u.ApplyUpdate(context.Background(), mm, a); err == nil {
		t.Fatal("sha256 mismatch should refuse to swap")
	}
	// The swap must NOT have happened.
	if got := readFile(t, exe); got != "OLD" {
		t.Fatalf("binary was swapped despite sha256 mismatch: %q", got)
	}
	if st, _ := LoadState(dir); st != nil {
		t.Fatalf("state left behind after refused swap: %+v", st)
	}
}

// TestBootCheckRollback exercises the full boot-count rollback dance with a real
// on-disk dummy binary: after 3 failed trial boots the 4th restores the previous
// binary and re-execs it.
func TestBootCheckRollback(t *testing.T) {
	dir := t.TempDir()
	exe := filepath.Join(dir, "filearr-agent")
	prev := PreviousBinaryPath(exe)
	writeFile(t, exe, "BROKEN-NEW")
	writeFile(t, prev, "GOOD-OLD")
	if err := SaveState(dir, State{NewVersion: "1.5.0", PreviousBinaryPath: prev, Attempts: 0, MaxAttempts: 3}); err != nil {
		t.Fatal(err)
	}

	newUp := func() *Updater {
		return New(Config{
			DataDir: dir, ExePath: exe, CurrentVersion: "1.5.0",
			reExec: func(string, []string) error { return nil },
			exit:   func(int) {},
		})
	}
	// 3 trial boots: each returns healthPending, none clears state.
	for boot := 1; boot <= 3; boot++ {
		pending, err := newUp().BootCheck(context.Background())
		if err != nil || !pending {
			t.Fatalf("boot %d: pending=%v err=%v", boot, pending, err)
		}
	}
	// 4th boot: rollback.
	pending, err := newUp().BootCheck(context.Background())
	if err != nil {
		t.Fatalf("boot 4: %v", err)
	}
	if pending {
		t.Fatal("boot 4 should have rolled back, not started a health window")
	}
	if got := readFile(t, exe); got != "GOOD-OLD" {
		t.Fatalf("rollback did not restore previous binary: current=%q", got)
	}
	if st, _ := LoadState(dir); st != nil {
		t.Fatalf("state not cleared after rollback: %+v", st)
	}
}

func TestConfirmHealthyClearsStateAndDeletesOld(t *testing.T) {
	pub, priv := testKeypair(t)
	fc := &fakeCentral{t: t, manifest: Manifest{}} // 204 on the version report
	srv := httptest.NewServer(fc.handler())
	defer srv.Close()
	_ = priv

	dir := t.TempDir()
	exe := filepath.Join(dir, "filearr-agent")
	prev := PreviousBinaryPath(exe)
	writeFile(t, exe, "NEW")
	writeFile(t, prev, "OLD")
	if err := SaveState(dir, State{NewVersion: "1.5.0", PreviousBinaryPath: prev, Attempts: 1, MaxAttempts: 3}); err != nil {
		t.Fatal(err)
	}
	u := buildUpdater(t, srv, pub, dir, exe, "1.5.0", "linux", "amd64", nil)
	u.ConfirmHealthy(context.Background())

	if st, _ := LoadState(dir); st != nil {
		t.Fatalf("state not cleared after confirm: %+v", st)
	}
	if _, err := os.Stat(prev); !os.IsNotExist(err) {
		t.Fatalf("previous (.old) binary not deleted: err=%v", err)
	}
	if fc.lastCur != "1.5.0" {
		t.Fatalf("confirm did not report running version: %q", fc.lastCur)
	}
}
