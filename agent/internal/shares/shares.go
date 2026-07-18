// Package shares is the agent's best-effort network-share discovery (P10-T11,
// docs/tasks/phase-10-agent-transfer-tasks.md §P10-T11). It answers ONE
// question: "if a local absolute file path is exported over a network share, how
// does a remote client open it?" — returning a UNC (\\host\share\rel) and/or a
// URL (smb://host/share/rel, nfs://host/export/rel) *hint* that central attaches
// to the replicated item so the UI can render a network-open link.
//
// # Best-effort by construction (Architect ruling R1)
//
// Discovery is advisory only. Anonymous-share visibility, permission-scoped
// enumeration, multi-homed hosts, and locked-down shells mean a MISSING hint is
// the normal case, never an error: every enumeration failure yields no exports
// (and therefore no hint), never a propagated error, so a file simply falls
// through to the central mapping fallback (P10-T12). Ambiguity is resolved the
// same honest way — if two equally-specific exports cover a path we return NO
// hint rather than guess (see Resolver.Hint).
//
// # Per-OS enumeration (see enumerate.go)
//
//   - Windows: PowerShell `Get-SmbShare` → CSV (dependable quoting for paths with
//     spaces, unlike `net share`'s space-ambiguous columns), falling back to
//     `net share` when PowerShell is unavailable/locked down.
//   - Linux:   /etc/samba/smb.conf `[share]` `path =` sections (SMB) + /etc/exports
//     (NFS).
//   - macOS:   `sharing -l` share points (SMB).
//
// The pure PARSERS (parse.go) are platform-neutral and fixture-tested; only the
// exec/file-read dispatch (enumerate.go) is OS-specific, and it compiles on every
// target (runtime.GOOS switch, no build tags, no cgo).
//
// # Caching
//
// Enumeration shells out / reads files, so results are cached for a TTL (default
// 5 min): a scan touching thousands of files enumerates at most once per window.
package shares

import (
	"os"
	"runtime"
	"strings"
	"sync"
	"time"
)

// DefaultTTL bounds how often enumeration shells out. A share topology changes
// rarely; a few minutes of staleness is immaterial for a display hint.
const DefaultTTL = 5 * time.Minute

// Hint is the resolved network location for one local path — the exact additive
// share_hint object the agent attaches to a replicated event (P10-T11). ShareURL
// is always set on a non-nil hint; UNC/ShareName are empty for non-SMB (NFS)
// exports. Source is always "agent" (agent-discovered), distinguishing it on the
// wire from a central-mapping-derived location.
type Hint struct {
	ShareURL  string
	UNC       string
	ShareName string
	Host      string
	Source    string // always "agent"
}

// export is one discovered network export: a local absolute path made reachable
// under a share name (SMB) or as an NFS export.
type export struct {
	name string // SMB share name; "" for NFS
	path string // local absolute path exported
	kind string // "smb" | "nfs"
}

// Resolver enumerates the host's network shares (cached) and maps a local
// absolute path to a Hint. The zero value is not usable — call New.
type Resolver struct {
	host     string
	ttl      time.Duration
	caseFold bool // fold path case (Windows) when matching exports
	now      func() time.Time
	enum     func() []export // injectable in tests; defaults to enumerateOS

	mu       sync.Mutex
	cached   []export
	loadedAt time.Time
	loaded   bool
}

// New builds a Resolver for host (the name rendered into every hint — normally
// os.Hostname(), or a config override). An empty host falls back to os.Hostname()
// and finally "localhost", so a hint always carries *some* host.
func New(host string) *Resolver {
	if host == "" {
		if h, err := os.Hostname(); err == nil {
			host = h
		}
	}
	if host == "" {
		host = "localhost"
	}
	r := &Resolver{
		host:     host,
		ttl:      DefaultTTL,
		caseFold: runtime.GOOS == "windows",
		now:      time.Now,
	}
	r.enum = func() []export { return enumerateOS() }
	return r
}

// exports returns the cached export set, (re)enumerating when the TTL has lapsed.
// Enumeration never errors (R1): a failure yields an empty set that is cached for
// the TTL like any other, so a locked-down host does not re-shell every call.
func (r *Resolver) exports() []export {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.loaded && r.now().Sub(r.loadedAt) < r.ttl {
		return r.cached
	}
	r.cached = r.enum()
	r.loadedAt = r.now()
	r.loaded = true
	return r.cached
}

// Hint resolves absPath to a network-open Hint, or nil when no share covers it
// (the normal, non-error case — R1). Selection is longest-export-path-wins on
// segment boundaries (the same discipline as the central resolve_share_url); a
// TIE between two distinct equally-specific exports is treated as multi-homed
// AMBIGUITY and returns nil rather than fabricate a guess.
func (r *Resolver) Hint(absPath string) *Hint {
	if absPath == "" {
		return nil
	}
	target := normPath(absPath, r.caseFold)
	exports := r.exports()

	var best *export
	bestLen := -1
	tie := false
	for i := range exports {
		e := &exports[i]
		base := normPath(e.path, r.caseFold)
		if !covers(base, target) {
			continue
		}
		switch {
		case len(base) > bestLen:
			best, bestLen, tie = e, len(base), false
		case len(base) == bestLen && best != nil && !sameExport(*best, *e):
			tie = true
		}
	}
	if best == nil || tie {
		return nil // no covering export, or ambiguous multi-homed coverage
	}
	baseSegs := splitSegs(normPath(best.path, r.caseFold))
	// Recompute the remainder from the ORIGINAL (case-preserving) path so the hint
	// keeps the real filename case, not the case-folded compare key.
	origSegs := splitSegs(normPath(absPath, false))
	rel := origSegs[len(baseSegs):]
	return r.build(*best, rel)
}

// build renders the UNC + URL forms for a covering export and its remainder
// segments. Segments are joined with the native separator (backslash for UNC,
// forward slash for URL); nothing is percent-encoded (a display/open path, not an
// href), mirroring the central _join_share discipline.
func (r *Resolver) build(e export, rel []string) *Hint {
	switch e.kind {
	case "nfs":
		// NFS has no UNC form; the URL is nfs://host/<export-path>/<rel>.
		segs := append([]string{r.host}, splitSegs(normPath(e.path, false))...)
		segs = append(segs, rel...)
		return &Hint{
			ShareURL: "nfs://" + strings.Join(segs, "/"),
			Host:     r.host,
			Source:   "agent",
		}
	default: // "smb"
		urlSegs := append([]string{r.host, e.name}, rel...)
		uncSegs := append([]string{e.name}, rel...)
		return &Hint{
			ShareURL:  "smb://" + strings.Join(urlSegs, "/"),
			UNC:       `\\` + r.host + `\` + strings.Join(uncSegs, `\`),
			ShareName: e.name,
			Host:      r.host,
			Source:    "agent",
		}
	}
}

// --- pure path helpers (mirror central _norm_local / segment-boundary cover) ---

// normPath converts backslashes to forward slashes and strips surrounding
// slashes; when fold is set it also lowercases (Windows path case-insensitivity).
func normPath(p string, fold bool) string {
	p = strings.ReplaceAll(p, `\`, "/")
	p = strings.Trim(p, "/")
	if fold {
		p = strings.ToLower(p)
	}
	return p
}

func splitSegs(norm string) []string {
	if norm == "" {
		return nil
	}
	return strings.Split(norm, "/")
}

// covers reports whether base (an export path) covers target (a file path) on
// segment boundaries: equal, base is a root (empty), or target is under base.
func covers(base, target string) bool {
	return base == "" || target == base || strings.HasPrefix(target, base+"/")
}

func sameExport(a, b export) bool {
	return a.name == b.name && a.path == b.path && a.kind == b.kind
}
