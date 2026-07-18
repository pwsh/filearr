// Package inventory is the extensible inventory framework (W6-D3): a generic
// `inventory` agent command that walks an expanded set of roots (via the shared
// internal/pathspec engine + the W6-R1 presets) and runs a set of registered
// per-file COLLECTORS, emitting one NDJSON record per surviving entry plus a
// UI-facing summary. New inventory COMPOSITIONS (a preset + collector set) are
// authored centrally and need no agent redeployment — the vocabulary the agent
// advertises (Capabilities) is what an admin composes against.
//
// Collectors are metadata-only by contract: they stat/query attributes but NEVER
// open file CONTENT (the cloud-placeholder hydration hazard, W6-R1 §1.2/§3.3).
package inventory

import (
	"context"
	"io/fs"
	"sort"
)

// CapabilityVersion is the inventory contract version the agent advertises. Bump
// it when the collector output shape or command payload contract changes in a way
// central must gate on.
const CapabilityVersion = 1

// Collector computes a set of metadata fields for one filesystem entry. Implementations
// MUST be metadata-only (no content reads). A returned error is per-file, per-collector
// and fail-soft: the runner records it and continues with the other collectors/entries.
type Collector interface {
	// Name is the stable identifier an admin composes against (matches a central
	// InventoryConfig.collectors entry).
	Name() string
	// Collect returns this collector's fields for path (info already stat'd by the
	// walk — reuse it, do not re-stat). ctx is honored for cancellation.
	Collect(ctx context.Context, path string, info fs.FileInfo) (map[string]any, error)
}

// Registry is an ordered, name-keyed set of collectors. It is the extension point:
// a future collector is Register()ed here (and, by capability advertisement,
// becomes composable centrally) with no other wiring.
type Registry struct {
	m     map[string]Collector
	order []string
}

// NewRegistry returns an empty registry.
func NewRegistry() *Registry {
	return &Registry{m: map[string]Collector{}}
}

// Register adds (or replaces) a collector, preserving first-registration order for
// a stable Names()/capability advertisement. Returns the registry for chaining.
func (r *Registry) Register(c Collector) *Registry {
	name := c.Name()
	if _, exists := r.m[name]; !exists {
		r.order = append(r.order, name)
	}
	r.m[name] = c
	return r
}

// Get resolves a collector by name.
func (r *Registry) Get(name string) (Collector, bool) {
	c, ok := r.m[name]
	return c, ok
}

// Names lists the registered collector names in registration order.
func (r *Registry) Names() []string {
	out := make([]string, len(r.order))
	copy(out, r.order)
	return out
}

// DefaultRegistry is the v1 built-in collector set: stat, owner, perms,
// placeholder. It is what the agent advertises and what a command's `collectors`
// list resolves against.
func DefaultRegistry() *Registry {
	return NewRegistry().
		Register(statCollector{}).
		Register(ownerCollector{}).
		Register(permsCollector{}).
		Register(placeholderCollector{})
}

// Capabilities is the additive advertisement the agent attaches to its command
// poll so central can store what this agent supports (and the UI can offer only
// composable collectors). Shape: {inventory_collectors: [...], inventory_version: N}.
func Capabilities() map[string]any {
	names := DefaultRegistry().Names()
	sorted := make([]string, len(names))
	copy(sorted, names)
	sort.Strings(sorted)
	return map[string]any{
		"inventory_collectors": sorted,
		"inventory_version":    CapabilityVersion,
	}
}
