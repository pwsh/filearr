package permissions

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/filearr/filearr/agent/internal/inventory"
)

// TestCollectIsInertScaffold verifies the collector never yields data — it
// returns the sentinel on every platform, so it cannot masquerade real ACLs.
func TestCollectIsInertScaffold(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "x.txt")
	if err := os.WriteFile(f, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Lstat(f)
	if err != nil {
		t.Fatal(err)
	}
	fields, err := Collector{}.Collect(context.Background(), f, fi)
	if !errors.Is(err, ErrPermissionsScaffold) {
		t.Fatalf("Collect err = %v, want ErrPermissionsScaffold", err)
	}
	if fields != nil {
		t.Fatalf("scaffold Collect must return nil fields, got %v", fields)
	}
}

func TestCollectorName(t *testing.T) {
	if (Collector{}).Name() != "permissions" || CollectorName != "permissions" {
		t.Fatalf("name mismatch: %q / %q", (Collector{}).Name(), CollectorName)
	}
}

// TestScaffoldSatisfiesCollectorInterface is a compile-time + runtime assertion
// that the scaffold type IS a valid inventory.Collector (so the W7 registration
// seam is real), WITHOUT registering it.
func TestScaffoldSatisfiesCollectorInterface(t *testing.T) {
	var c inventory.Collector = Collector{}
	if c.Name() != CollectorName {
		t.Fatalf("interface name: %q", c.Name())
	}
}

// TestPermissionsNotAdvertised is the binding guard: the scaffold must NOT be in
// the default registry nor the advertised capability set. Central must never
// offer or run it until the per-OS reads land (the W7 registration seam).
func TestPermissionsNotAdvertised(t *testing.T) {
	if _, ok := inventory.DefaultRegistry().Get(CollectorName); ok {
		t.Fatalf("%q must NOT be in DefaultRegistry while the scaffold is inert", CollectorName)
	}
	caps := inventory.Capabilities()
	advertised, _ := caps["inventory_collectors"].([]string)
	for _, n := range advertised {
		if n == CollectorName {
			t.Fatalf("%q must NOT be advertised in Capabilities: %v", CollectorName, advertised)
		}
	}
}
