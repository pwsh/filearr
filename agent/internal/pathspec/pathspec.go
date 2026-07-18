// Package pathspec is the SHARED path-expansion engine (W6-D3): it turns a
// stored path spec — env tokens, a home tilde, glob segments — into the concrete
// set of filesystem roots the agent will walk. It is deliberately decoupled from
// the inventory framework that first consumes it so the group scan-root policy
// (W6-D2 scan_selections) can expand roots with the SAME code.
//
// Resolution order (documented, load-bearing):
//
//  1. env-token expansion — Windows `%VAR%`, POSIX `$VAR` / `${VAR}`, and a
//     leading `~` (home). An unset variable makes the whole spec fail (recorded
//     per-spec, never a silent walk of a literal `%UNSET%\x`).
//  2. glob expansion — a spec carrying glob metacharacters (`*`, `?`, `[`) is
//     expanded with stdlib filepath.Glob semantics (which is existence-filtered
//     by nature, and is how per-user fan-out like `/home/*/documents` or
//     `C:\Users\*\Documents` resolves). A spec with no metacharacters is returned
//     as a single literal root (existence is decided later, by the walk).
//  3. a global fan-out cap (default DefaultMaxRoots) bounds a hostile/pathological
//     spec set; hitting it truncates and flags Truncated rather than erroring the
//     whole run.
//
// Regexp include/exclude filtering (RE2, applied to rel paths during the walk) is
// a separate concern — see Filter.
package pathspec

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// DefaultMaxRoots caps the total number of expanded roots across a whole spec set
// — the fan-out guard against a spec like `C:\Users\*\*\*` on a huge host.
const DefaultMaxRoots = 10000

// Expander resolves path specs to concrete roots. The zero value is usable (it
// wires the real OS env/home + DefaultMaxRoots); tests inject Getenv/Home to make
// expansion deterministic on any host.
type Expander struct {
	// MaxRoots overrides DefaultMaxRoots when > 0.
	MaxRoots int
	// Getenv looks up an environment variable (name, ok). nil => os.LookupEnv.
	Getenv func(string) (string, bool)
	// Home resolves the current user's home directory. nil => os.UserHomeDir.
	Home func() (string, error)
}

// Result is the outcome of expanding a spec set: the deduped roots, whether the
// fan-out cap truncated them, and any per-spec expansion errors (unset variable,
// bad glob) — errors are surfaced, never silently dropped.
type Result struct {
	Roots     []string
	Truncated bool
	// Errors maps a spec string to the reason it produced no roots. A spec may
	// legitimately expand to zero roots (a glob matching nothing) WITHOUT an
	// error entry.
	Errors map[string]string
}

func (e *Expander) getenv() func(string) (string, bool) {
	if e.Getenv != nil {
		return e.Getenv
	}
	return os.LookupEnv
}

func (e *Expander) home() func() (string, error) {
	if e.Home != nil {
		return e.Home
	}
	return os.UserHomeDir
}

func (e *Expander) maxRoots() int {
	if e.MaxRoots > 0 {
		return e.MaxRoots
	}
	return DefaultMaxRoots
}

// Expand resolves a spec set to the deduped root set. Specs are processed in
// order; duplicates (across specs and glob matches) collapse to the first
// occurrence, preserving a stable, deterministic root order.
func (e *Expander) Expand(specs []string) Result {
	res := Result{}
	seen := map[string]bool{}
	cap := e.maxRoots()
	for _, spec := range specs {
		roots, err := e.expandOne(spec)
		if err != nil {
			if res.Errors == nil {
				res.Errors = map[string]string{}
			}
			res.Errors[spec] = err.Error()
			continue
		}
		for _, r := range roots {
			if seen[r] {
				continue
			}
			if len(res.Roots) >= cap {
				res.Truncated = true
				return res
			}
			seen[r] = true
			res.Roots = append(res.Roots, r)
		}
	}
	return res
}

// expandOne resolves a single spec: env-token expansion then (conditional) glob.
func (e *Expander) expandOne(spec string) ([]string, error) {
	spec = strings.TrimSpace(spec)
	if spec == "" {
		return nil, fmt.Errorf("empty spec")
	}
	expanded, err := expandTokens(spec, e.getenv(), e.home())
	if err != nil {
		return nil, err
	}
	// Convert POSIX-style separators to native so a lenient spec globs correctly
	// on either OS, then clean.
	native := filepath.Clean(filepath.FromSlash(expanded))
	if !hasGlobMeta(native) {
		return []string{native}, nil
	}
	matches, err := filepath.Glob(native)
	if err != nil {
		return nil, fmt.Errorf("glob %q: %w", native, err)
	}
	// filepath.Glob already sorts, but clean each result for a stable, comparable
	// root string (dedup in Expand keys on it).
	out := make([]string, 0, len(matches))
	for _, m := range matches {
		out = append(out, filepath.Clean(m))
	}
	sort.Strings(out)
	return out, nil
}

// hasGlobMeta reports whether p contains a filepath.Glob metacharacter. Brace
// (`{}`) is intentionally NOT treated as magic: Go's filepath.Glob has no brace
// expansion, so a `{a,b}` spec resolves literally (central only balance-checks
// braces; the agent honors stdlib semantics — documented divergence).
func hasGlobMeta(p string) bool {
	return strings.ContainsAny(p, "*?[")
}
