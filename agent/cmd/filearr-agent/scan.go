package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/query"
	"github.com/filearr/filearr/agent/internal/scan"
	"github.com/filearr/filearr/agent/internal/shares"
)

// envShareHost overrides the hostname rendered into a P10-T11 share hint
// (\\host\share, smb://host/...). Empty falls back to os.Hostname() — set it when
// clients reach this machine by a different name (a DNS alias, a NAS identity).
const envShareHost = "FILEARR_AGENT_SHARE_HOST"

// scanConfigName is the persistent scan configuration under the data dir. It
// records the roots, effective presets, and the content-hash ceiling so a
// scheduled/`--watch` run reproduces the same walk without re-passing flags.
const scanConfigName = "scan.json"

// indexDBName is the local SQLite catalog under the data dir.
const indexDBName = "index.db"

// historyDBName is the LOCAL-ONLY query frecency store (P7-T6), a SEPARATE SQLite
// file from index.db so the outbox/replication path (which only ever holds the
// index handle) is architecturally incapable of touching it. Wiping the agent's
// data dir wipes this file too — and with it, all local search history.
const historyDBName = "history.db"

// scanConfig is the on-disk form of a scan setup (persisted as scan.json).
type scanConfig struct {
	Roots          []string `json:"roots"`
	Presets        []string `json:"presets,omitempty"`
	ExcludeGlobs   []string `json:"exclude_globs,omitempty"`
	IncludeGlobs   []string `json:"include_globs,omitempty"`
	EnabledTypes   []string `json:"enabled_types,omitempty"`
	ContentCeiling int64    `json:"content_ceiling_bytes,omitempty"`
}

// stringSlice is a repeatable string flag (e.g. -root a -root b).
type stringSlice []string

func (s *stringSlice) String() string { return strings.Join(*s, ",") }
func (s *stringSlice) Set(v string) error {
	*s = append(*s, v)
	return nil
}

// runScan implements `filearr-agent scan --root <path>... [--watch]`.
func runScan(args []string) error {
	fs := newFlagSet("scan")
	cfg := bindCommonFlags(fs)
	var roots stringSlice
	fs.Var(&roots, "root", "root directory to scan (repeatable)")
	watch := fs.Bool("watch", false, "keep watching the roots and rescan on change (settle-coalesced)")
	settle := fs.Duration("settle", scan.DefaultSettle, "watch settle window before a coalesced rescan")
	if err := fs.Parse(args); err != nil {
		return err
	}

	sc, err := loadOrInitScanConfig(cfg.DataDir, roots)
	if err != nil {
		return err
	}
	if len(sc.Roots) == 0 {
		return fmt.Errorf("no roots configured (pass -root or add them to %s)", filepath.Join(cfg.DataDir, scanConfigName))
	}

	// Central policy (P5-T6) overlays scan.json: for the keys it sets, policy WINS
	// (documented precedence), so one-shot `scan` invocations honor central config
	// the same way the daemon does. A false watch_mode gates --watch off.
	sc, watchDisabled := applyPolicyToScan(cfg.DataDir, sc, watch)
	if watchDisabled {
		fmt.Fprintln(os.Stderr, "central policy sets watch_mode=false; --watch disabled")
	}

	store, err := index.Open(filepath.Join(cfg.DataDir, indexDBName))
	if err != nil {
		return fmt.Errorf("open local index: %w", err)
	}
	defer store.Close()
	if store.Rebuilt {
		fmt.Fprintln(os.Stderr, "index.db failed integrity_check — deleted and rebuilt from scratch; a full rescan repopulates it")
	}

	ctx, cancel := signalContext()
	defer cancel()

	opts := scan.Options{
		EnabledPresets: sc.Presets,
		ExcludeGlobs:   sc.ExcludeGlobs,
		IncludeGlobs:   sc.IncludeGlobs,
		EnabledTypes:   sc.EnabledTypes,
		Hash:           hashPolicy(sc),
		Progress: func(p scan.Progress) {
			fmt.Printf("  ... seen=%d new=%d changed=%d\n", p.Seen, p.New, p.Changed)
		},
		// P10-T11 best-effort share discovery: attach a network-open hint to each
		// created/modified item when a local share covers its path. A single
		// resolver (5-min TTL cache) is shared across all roots.
		Shares: shares.New(os.Getenv(envShareHost)),
	}

	scanAll := func() {
		for _, root := range sc.Roots {
			o := opts
			o.Root = root
			res, err := scan.Scan(ctx, store, o)
			if err != nil {
				fmt.Fprintf(os.Stderr, "scan %s: %v\n", root, err)
				continue
			}
			reportScan(root, res)
		}
	}

	scanAll()
	if !*watch {
		return nil
	}

	fmt.Printf("watching %d root(s) (settle %s); Ctrl-C to stop\n", len(sc.Roots), *settle)
	return watchRoots(ctx, sc.Roots, *settle, scanAll)
}

// watchRoots runs a settle-coalesced watcher per root, each triggering a full
// rescan of all roots on a settled burst.
func watchRoots(ctx context.Context, roots []string, settle time.Duration, rescan func()) error {
	errc := make(chan error, len(roots))
	for _, root := range roots {
		go func(r string) {
			errc <- scan.Watch(ctx, r, settle, rescan)
		}(root)
	}
	// Block until ctx is cancelled; report the first non-cancel error.
	<-ctx.Done()
	return nil
}

// applyPolicyToScan overlays the cached central policy onto sc (policy wins for
// the keys it sets) and gates --watch: when the policy sets watch_mode=false it
// flips *watch off and reports watchDisabled=true. With no cached policy (or a
// parse error) sc and *watch are returned unchanged — the agent falls back to
// its local scan.json, never failing a scan on a missing/broken policy.
func applyPolicyToScan(dataDir string, sc scanConfig, watch *bool) (out scanConfig, watchDisabled bool) {
	pol, ok, err := agentcfg.LoadCachedPolicy(dataDir)
	if err != nil || !ok {
		return sc, false
	}
	overlaid := pol.OverlayScan(agentcfg.ScanSettings{
		Presets:             sc.Presets,
		IncludeGlobs:        sc.IncludeGlobs,
		ExcludeGlobs:        sc.ExcludeGlobs,
		ContentCeilingBytes: sc.ContentCeiling,
	})
	sc.Presets = overlaid.Presets
	sc.IncludeGlobs = overlaid.IncludeGlobs
	sc.ExcludeGlobs = overlaid.ExcludeGlobs
	sc.ContentCeiling = overlaid.ContentCeilingBytes

	if watch != nil && *watch {
		if allowed, set := pol.WatchAllowed(); set && !allowed {
			*watch = false
			watchDisabled = true
		}
	}
	return sc, watchDisabled
}

// runSearch implements `filearr-agent search <query>` over the full local query
// DSL (agent/internal/query), executed on a dedicated read-only connection. The
// typo-tolerance is a LOCAL, bounded edit-distance re-rank (fires only on zero
// exact hits or an explicit ~ term) — it is NOT central's Meilisearch ranking;
// results may differ from the central UI.
//
// LEGACY PATH: `search` opens the index file directly, bypassing the P7-T2 policy
// gate + peer-credential boundary. The SUPPORTED local query surface is
// `filearr-agent query`, which dials the agent's socket/pipe. Prefer it.
func runSearch(args []string) error {
	fs := newFlagSet("search")
	cfg := bindCommonFlags(fs)
	includeSidecars := fs.Bool("sidecars", false, "include sidecar rows (hidden by default)")
	limit := fs.Int("limit", 50, "max results")
	asJSON := fs.Bool("json", false, "emit one JSON object per result line (NDJSON)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	raw := strings.TrimSpace(strings.Join(fs.Args(), " "))
	if raw == "" {
		return fmt.Errorf("usage: filearr-agent search [--json] [--limit N] [--sidecars] <query>\n  (legacy direct-index path; the supported surface is `filearr-agent query`)")
	}

	searcher, err := query.NewSearcher(filepath.Join(cfg.DataDir, indexDBName))
	if err != nil {
		return fmt.Errorf("open local index (read-only): %w", err)
	}
	defer searcher.Close()

	results, err := searcher.Search(context.Background(), raw, *includeSidecars, *limit)
	if err != nil {
		var pe *query.ParseError
		var ee *query.ExecError
		switch {
		case errors.As(err, &pe):
			return fmt.Errorf("query syntax error [%s] at position %d: %s", pe.Code, pe.Position, pe.Reason)
		case errors.As(err, &ee):
			return fmt.Errorf("query not runnable locally [%s]: %s", ee.Code, ee.Message)
		default:
			return err
		}
	}

	fuzzy := false
	for _, r := range results {
		if r.FuzzyMatched {
			fuzzy = true
		}
		if *asJSON {
			row := map[string]any{
				"id":            r.Item.ID,
				"rel_path":      r.Item.RelPath,
				"filename":      r.Item.Filename,
				"extension":     r.Item.Extension,
				"size":          r.Item.Size,
				"mtime_ns":      r.Item.MtimeNs,
				"media_type":    r.Item.MediaType,
				"status":        r.Item.Status,
				"fuzzy_matched": r.FuzzyMatched,
				"score":         r.Score,
			}
			buf, _ := json.Marshal(row)
			fmt.Println(string(buf))
			continue
		}
		flag := ""
		if r.FuzzyMatched {
			flag = fmt.Sprintf("\t~fuzzy(%d)", r.Score)
		}
		fmt.Printf("%s\t%d\t%s%s\n", r.Item.RelPath, r.Item.Size, r.Item.Status, flag)
	}
	if len(results) == 0 {
		fmt.Fprintln(os.Stderr, "no matches")
	} else if fuzzy {
		fmt.Fprintln(os.Stderr, "note: results include local typo-tolerant (fuzzy) matches; central search may rank differently")
	}
	return nil
}

// loadOrInitScanConfig reads scan.json, merges any -root flags (which win and are
// persisted), and writes it back. A first run with -root creates the file.
func loadOrInitScanConfig(dataDir string, roots []string) (scanConfig, error) {
	path := filepath.Join(dataDir, scanConfigName)
	var sc scanConfig
	if buf, err := os.ReadFile(path); err == nil {
		if err := json.Unmarshal(buf, &sc); err != nil {
			return sc, fmt.Errorf("parse %s: %w", path, err)
		}
	} else if !os.IsNotExist(err) {
		return sc, fmt.Errorf("read %s: %w", path, err)
	}

	if len(roots) > 0 {
		sc.Roots = mergeAbs(sc.Roots, roots)
		if err := writeScanConfig(path, sc); err != nil {
			return sc, err
		}
	}
	return sc, nil
}

// mergeAbs unions existing roots with new (absolutised) ones, order-preserving.
func mergeAbs(existing, added []string) []string {
	seen := map[string]bool{}
	var out []string
	add := func(p string) {
		if abs, err := filepath.Abs(p); err == nil {
			p = abs
		}
		if !seen[p] {
			seen[p] = true
			out = append(out, p)
		}
	}
	for _, p := range existing {
		add(p)
	}
	for _, p := range added {
		add(p)
	}
	return out
}

func writeScanConfig(path string, sc scanConfig) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("create data dir: %w", err)
	}
	buf, err := json.MarshalIndent(sc, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(buf, '\n'), 0o644)
}

func hashPolicy(sc scanConfig) scan.HashPolicy {
	if sc.ContentCeiling > 0 {
		return scan.HashPolicy{ComputeContent: true, FullMaxBytes: sc.ContentCeiling}
	}
	return scan.DefaultHashPolicy()
}

func reportScan(root string, res scan.Result) {
	if res.ScopeMissing {
		fmt.Printf("scan %s: scope missing, nothing written\n", root)
		return
	}
	status := "finished"
	if res.Stopped {
		status = "stopped (graceful)"
	}
	fmt.Printf("scan %s: %s — seen=%d new=%d changed=%d missing=%d moved=%d ambiguous=%d sidecars=%d linked=%d\n",
		root, status, res.Seen, res.New, res.Changed, res.Missing, res.Moved, res.MoveAmbiguous,
		res.Sidecars.Sidecars, res.Sidecars.Linked)
}
