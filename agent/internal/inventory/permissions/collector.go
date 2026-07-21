package permissions

import (
	"context"
	"errors"
	"io/fs"
)

// CollectorName is the stable name the (future) real collector advertises and an
// admin composes against. It is intentionally distinct from the existing
// summary-only "perms" collector (brief §0).
const CollectorName = "permissions"

// ErrPermissionsScaffold is the sentinel every inert boundary returns. It makes
// the scaffold's incompleteness LOUD: the collector never yields wrong or empty
// data masquerading as a real, verified "no permissions found" result. The real
// per-OS reads (W7-T2/T3/T4) replace the stubs that return it.
var ErrPermissionsScaffold = errors.New(
	"permissions collector is scaffold-only: per-OS ACL reads not implemented (W7)")

// Collector is the full-ACE permissions collector. It satisfies the parent
// package's inventory.Collector interface structurally (Name + Collect) WITHOUT
// importing it, so registering it later creates no import cycle.
//
// W7: register in DefaultRegistry once the per-OS reads are implemented.
//
// Until then this type is DELIBERATELY NOT added to
// inventory.DefaultRegistry() and therefore NOT advertised in
// inventory.Capabilities(): central must never offer or run it. The scaffold
// compiles, vets, and is importable, but Collect is inert.
type Collector struct{}

// Name returns the stable collector identifier.
func (Collector) Name() string { return CollectorName }

// Collect is INERT in the scaffold: it routes through the per-OS read seam
// (collectRecord), which returns ErrPermissionsScaffold on every platform. When
// the real reads land, collectRecord returns a populated *Record and this method
// flattens it into the inventory walk's per-entry map. The error is per-file and
// fail-soft by the runner's contract, so an un-registered scaffold could never
// corrupt a run even if it were wired.
func (Collector) Collect(_ context.Context, path string, info fs.FileInfo) (map[string]any, error) {
	if _, err := collectRecord(path, info); err != nil {
		return nil, err
	}
	// Unreachable in the scaffold (collectRecord always errors). Shape retained so
	// the emit path is obvious to the W7 implementer.
	return nil, ErrPermissionsScaffold
}
