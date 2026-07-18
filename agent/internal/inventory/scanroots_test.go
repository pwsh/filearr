package inventory

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestExpandScanSelectionsSeam(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "docs"), 0o755); err != nil {
		t.Fatal(err)
	}
	// A policy body carrying a W6-D2 group.scan_selections section: one enabled
	// selection (custom preset + an explicit glob) and one disabled selection.
	disabled := false
	policy := map[string]any{
		"group": map[string]any{
			"scan_selections": []any{
				map[string]any{
					"preset": "custom",
					"paths":  []any{filepath.Join(root, "*")},
				},
				map[string]any{
					"paths":   []any{filepath.Join(root, "ignored")},
					"enabled": disabled,
				},
			},
		},
	}
	raw, _ := json.Marshal(policy)

	res := ExpandScanSelections(fakeHostLinux(), raw)
	if res.SelectionsCount != 1 {
		t.Fatalf("selections consumed: %d", res.SelectionsCount)
	}
	if len(res.Roots) != 1 || filepath.Base(res.Roots[0]) != "docs" {
		t.Fatalf("roots: %v", res.Roots)
	}
}

func TestExpandScanSelectionsEmpty(t *testing.T) {
	if res := ExpandScanSelections(fakeHostLinux(), nil); res.SelectionsCount != 0 {
		t.Fatalf("nil policy should consume nothing: %+v", res)
	}
	if res := ExpandScanSelections(fakeHostLinux(), []byte(`{"policy":{}}`)); res.SelectionsCount != 0 {
		t.Fatalf("no group section should consume nothing: %+v", res)
	}
}

// seamHost is a minimal pathspec.Host for the seam test (custom preset + explicit
// paths need no OS facts beyond GOOS).
type seamHost struct{}

func (seamHost) GOOS() string                      { return "linux" }
func (seamHost) Home() (string, error)             { return "", nil }
func (seamHost) KnownFolder(string) (string, bool) { return "", false }
func (seamHost) Profiles() []string                { return nil }
func (seamHost) UserDirs(string) map[string]string { return map[string]string{} }

func fakeHostLinux() seamHost { return seamHost{} }
