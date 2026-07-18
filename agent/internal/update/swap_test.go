package update

import (
	"os"
	"path/filepath"
	"testing"
)

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o755); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(b)
}

func TestApplyAndRestore(t *testing.T) {
	dir := t.TempDir()
	current := filepath.Join(dir, "filearr-agent")
	newBin := filepath.Join(dir, "download", "new")
	writeFile(t, current, "OLD")
	if err := os.MkdirAll(filepath.Dir(newBin), 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, newBin, "NEW")

	prev, err := Apply(newBin, current)
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	if got := readFile(t, current); got != "NEW" {
		t.Fatalf("after apply current=%q, want NEW", got)
	}
	if got := readFile(t, prev); got != "OLD" {
		t.Fatalf("previous binary=%q, want OLD", got)
	}
	if prev != PreviousBinaryPath(current) {
		t.Fatalf("previous path %q != PreviousBinaryPath %q", prev, PreviousBinaryPath(current))
	}

	// Now roll back: restore the previous over the (broken) current.
	if err := Restore(prev, current); err != nil {
		t.Fatalf("restore: %v", err)
	}
	if got := readFile(t, current); got != "OLD" {
		t.Fatalf("after restore current=%q, want OLD", got)
	}
}

func TestApplyCrossVolumeFallback(t *testing.T) {
	// moveInto must fall back to copy when rename is not usable. We can't force
	// EXDEV portably, but a copyFile round-trip is exercised directly.
	dir := t.TempDir()
	src := filepath.Join(dir, "src")
	dst := filepath.Join(dir, "dst")
	writeFile(t, src, "PAYLOAD")
	if err := copyFile(src, dst); err != nil {
		t.Fatalf("copyFile: %v", err)
	}
	if got := readFile(t, dst); got != "PAYLOAD" {
		t.Fatalf("copied=%q, want PAYLOAD", got)
	}
}
