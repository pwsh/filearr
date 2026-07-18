package inventory

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"

	"github.com/filearr/filearr/agent/internal/pathspec"
	"github.com/filearr/filearr/agent/internal/scan"
)

// Defaults.
const (
	// InlineMaxBytes is the encoded-NDJSON ceiling for inlining the entries in the
	// command completion result JSONB; a larger result is gzipped and uploaded to
	// the inventory-results endpoint, with the completion carrying only a ref.
	InlineMaxBytes = 256 * 1024
	// DefaultMaxEntries bounds the total records a single inventory run collects.
	DefaultMaxEntries = 100000
	// deniedSampleCap bounds the sampled denied-path list in the summary.
	deniedSampleCap = 20
	// collectorErrorCap bounds the distinct collector-error strings in the summary.
	collectorErrorCap = 32
)

// exclusionPresets are the vetted W6-R1 exclusion bundles the inventory walk
// reuses (NOT forked): system/OS junk, caches, dev build noise. hidden_dotfiles is
// added by BuildLibrarySpec (it is default-enabled) — so dotfiles are pruned too.
var exclusionPresets = []string{"system_files", "os_metadata", "caches_temp", "node_modules_build"}

// Command is the decoded inventory command payload (the wire contract in
// api/agent_commands: {collectors, preset, paths, include_regex, exclude_regex,
// max_entries, max_depth}).
type Command struct {
	Collectors   []string
	Preset       string
	Paths        []string
	IncludeRegex []string
	ExcludeRegex []string
	MaxEntries   int
	MaxDepth     int // <= 0 => unlimited descent
}

// Summary is the always-present UI-facing digest (brief §6).
type Summary struct {
	RootsExpanded       int      `json:"roots_expanded"`
	Entries             int      `json:"entries"`
	Denied              int      `json:"denied"`
	PlaceholdersSkipped int      `json:"placeholders_skipped"`
	DurationMs          int64    `json:"duration_ms"`
	CollectorsRun       []string `json:"collectors_run"`
	CollectorErrors     []string `json:"collector_errors"`

	// Diagnostics (present only when non-trivial).
	RootsTruncated bool              `json:"roots_truncated,omitempty"`
	EntriesCapped  bool              `json:"entries_capped,omitempty"`
	DeniedSample   []string          `json:"denied_sample,omitempty"`
	ExpandErrors   map[string]string `json:"expand_errors,omitempty"`
	UnknownColl    []string          `json:"unknown_collectors,omitempty"`
}

// Result is a completed inventory run. Exactly one of Inline / Blob is meaningful:
// Inline (possibly empty, non-nil) when the encoded NDJSON fit the inline cap;
// Blob (gzip NDJSON) otherwise. NDJSONBytes is the uncompressed encoded size (the
// threshold input, surfaced for the caller's decision record).
type Result struct {
	Summary     Summary
	Inline      []map[string]any
	Blob        []byte
	NDJSONBytes int
}

// Inlineable reports whether this result is inline (vs an upload blob).
func (r Result) Inlineable() bool { return r.Blob == nil }

// Runner executes inventory commands. The zero value is NOT usable; use NewRunner.
type Runner struct {
	registry       *Registry
	host           pathspec.Host
	expander       *pathspec.Expander
	inlineMaxBytes int
	clock          func() time.Time
}

// NewRunner wires a Runner. A nil registry defaults to DefaultRegistry; a nil host
// to the real OS host; a nil expander to a default one.
func NewRunner(reg *Registry, host pathspec.Host) *Runner {
	return NewRunnerWithInlineCap(reg, host, InlineMaxBytes)
}

// NewRunnerWithInlineCap is NewRunner with an explicit inline-vs-upload byte
// threshold (tests shrink it to exercise the upload path without a huge tree).
func NewRunnerWithInlineCap(reg *Registry, host pathspec.Host, inlineCap int) *Runner {
	if reg == nil {
		reg = DefaultRegistry()
	}
	if host == nil {
		host = pathspec.OSHost()
	}
	if inlineCap <= 0 {
		inlineCap = InlineMaxBytes
	}
	return &Runner{
		registry:       reg,
		host:           host,
		expander:       &pathspec.Expander{},
		inlineMaxBytes: inlineCap,
		clock:          time.Now,
	}
}

// Run executes one inventory command: resolve the preset + explicit paths to
// roots, compile the include/exclude filter, build the exclusion spec, resolve the
// requested collectors (fail-soft on an unknown name), walk every root, and encode
// the result (inline vs upload decided by the encoded NDJSON size). A returned
// error is a whole-command failure (bad regex, no resolvable collectors); per-root
// / per-file / per-collector problems are accounted in the summary, never fatal.
func (r *Runner) Run(ctx context.Context, cmd Command) (Result, error) {
	start := r.clock()

	// 1. Roots: preset specs (per-OS) ++ explicit paths, expanded + deduped.
	var specs []string
	if cmd.Preset != "" {
		presetSpecs, err := pathspec.ResolvePreset(r.host, cmd.Preset)
		if err != nil {
			return Result{}, err
		}
		specs = append(specs, presetSpecs...)
	}
	specs = append(specs, cmd.Paths...)
	expanded := r.expander.Expand(specs)

	// 2. Regexp include/exclude filter (RE2). A bad pattern fails the command.
	filter, err := pathspec.CompileFilter(cmd.IncludeRegex, cmd.ExcludeRegex)
	if err != nil {
		return Result{}, err
	}

	// 3. Exclusion spec (reused W6-R1 bundles); 4. resolve collectors fail-soft.
	spec := scan.BuildLibrarySpec(exclusionPresets, nil, nil)
	collectors, unknown := r.resolveCollectors(cmd.Collectors)
	if len(collectors) == 0 {
		return Result{}, fmt.Errorf("no known collectors resolved from %v", cmd.Collectors)
	}

	maxEntries := cmd.MaxEntries
	if maxEntries <= 0 {
		maxEntries = DefaultMaxEntries
	}

	w := &walkState{
		ctx:        ctx,
		spec:       spec,
		filter:     filter,
		collectors: collectors,
		maxEntries: maxEntries,
		maxDepth:   cmd.MaxDepth,
	}
	for _, root := range expanded.Roots {
		if err := ctx.Err(); err != nil {
			return Result{}, err
		}
		w.walkRoot(root)
		if w.capped {
			break
		}
	}

	// Assemble the summary.
	sum := Summary{
		RootsExpanded:       len(expanded.Roots),
		Entries:             len(w.entries),
		Denied:              w.denied,
		PlaceholdersSkipped: w.placeholders,
		DurationMs:          r.clock().Sub(start).Milliseconds(),
		CollectorsRun:       collectorNames(collectors),
		CollectorErrors:     w.collectorErrs,
		RootsTruncated:      expanded.Truncated,
		EntriesCapped:       w.capped,
		DeniedSample:        w.deniedSample,
		ExpandErrors:        expanded.Errors,
		UnknownColl:         unknown,
	}
	if sum.CollectorErrors == nil {
		sum.CollectorErrors = []string{}
	}

	return encodeResult(sum, w.entries, r.inlineMaxBytes)
}

// resolveCollectors maps requested names to collectors in a STABLE registry order,
// returning the resolved set and the list of unknown names (fail-soft: an unknown
// name is reported, the rest still run).
func (r *Runner) resolveCollectors(names []string) ([]Collector, []string) {
	if len(names) == 0 {
		return nil, nil
	}
	want := map[string]bool{}
	for _, n := range names {
		want[n] = true
	}
	var out []Collector
	// Registry order for determinism.
	for _, n := range r.registry.Names() {
		if want[n] {
			if c, ok := r.registry.Get(n); ok {
				out = append(out, c)
			}
			delete(want, n)
		}
	}
	var unknown []string
	for n := range want {
		unknown = append(unknown, n)
	}
	sort.Strings(unknown)
	return out, unknown
}

func collectorNames(cs []Collector) []string {
	out := make([]string, len(cs))
	for i, c := range cs {
		out[i] = c.Name()
	}
	return out
}

// encodeResult marshals entries to NDJSON, measures it, and decides inline vs
// upload. An inline result carries the entry maps; an over-cap result gzips the
// NDJSON into Blob and leaves Inline nil.
func encodeResult(sum Summary, entries []map[string]any, inlineCap int) (Result, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	for _, e := range entries {
		if err := enc.Encode(e); err != nil { // Encode appends '\n' — NDJSON by construction
			return Result{}, fmt.Errorf("encode entry: %w", err)
		}
	}
	ndjson := buf.Bytes()
	res := Result{Summary: sum, NDJSONBytes: len(ndjson)}
	if len(ndjson) <= inlineCap {
		if entries == nil {
			entries = []map[string]any{}
		}
		res.Inline = entries
		return res, nil
	}
	blob, err := gzipBytes(ndjson)
	if err != nil {
		return Result{}, err
	}
	res.Blob = blob
	return res, nil
}

func gzipBytes(b []byte) ([]byte, error) {
	var out bytes.Buffer
	zw := gzip.NewWriter(&out)
	if _, err := zw.Write(b); err != nil {
		_ = zw.Close()
		return nil, err
	}
	if err := zw.Close(); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}

// walkState carries the mutable accounting across a multi-root walk.
type walkState struct {
	ctx        context.Context
	spec       *scan.Spec
	filter     *pathspec.Filter
	collectors []Collector
	maxEntries int
	maxDepth   int

	entries       []map[string]any
	denied        int
	deniedSample  []string
	placeholders  int
	capped        bool
	collectorErrs []string
	errSeen       map[string]bool
}

// frame is one directory pending descent, with its depth below the root.
type frame struct {
	rel   string
	depth int
}

// walkRoot performs the explicit-stack, prune-then-descend walk of one root,
// running collectors on every surviving file. Denied directories are counted +
// sampled (never silent); cloud placeholders are counted; max_depth / max_entries
// bound the walk.
func (w *walkState) walkRoot(root string) {
	stack := []frame{{rel: "", depth: 0}}
	for len(stack) > 0 {
		if w.ctx.Err() != nil || w.capped {
			return
		}
		fr := stack[len(stack)-1]
		stack = stack[:len(stack)-1]

		current := root
		if fr.rel != "" {
			current = filepath.Join(root, filepath.FromSlash(fr.rel))
		}
		entries, err := os.ReadDir(current)
		if err != nil {
			w.recordDenied(current)
			continue
		}
		for _, de := range entries {
			rel := de.Name()
			if fr.rel != "" {
				rel = fr.rel + "/" + de.Name()
			}
			abs := filepath.Join(current, de.Name())
			if de.IsDir() {
				if w.spec.PruneDir(rel, abs) {
					continue
				}
				if w.maxDepth > 0 && fr.depth+1 > w.maxDepth {
					continue // do not descend past the depth cap
				}
				stack = append(stack, frame{rel: rel, depth: fr.depth + 1})
				continue
			}
			// File-level exclusion (bundles) then the regex filter.
			if w.spec.MatchFile(rel) || !w.filter.Allow(rel) {
				continue
			}
			info, ierr := de.Info()
			if ierr != nil {
				continue // vanished mid-walk (race)
			}
			if isPlaceholder(info) {
				w.placeholders++
			}
			rec := map[string]any{"path": abs, "rel": rel}
			for _, c := range w.collectors {
				fields, cerr := c.Collect(w.ctx, abs, info)
				if cerr != nil {
					w.recordCollectorErr(c.Name(), cerr)
					continue
				}
				for k, v := range fields {
					rec[k] = v
				}
			}
			w.entries = append(w.entries, rec)
			if len(w.entries) >= w.maxEntries {
				w.capped = true
				return
			}
		}
	}
}

func (w *walkState) recordDenied(path string) {
	w.denied++
	if len(w.deniedSample) < deniedSampleCap {
		w.deniedSample = append(w.deniedSample, path)
	}
}

func (w *walkState) recordCollectorErr(name string, err error) {
	if w.errSeen == nil {
		w.errSeen = map[string]bool{}
	}
	msg := name + ": " + err.Error()
	if w.errSeen[msg] || len(w.collectorErrs) >= collectorErrorCap {
		return
	}
	w.errSeen[msg] = true
	w.collectorErrs = append(w.collectorErrs, msg)
}
