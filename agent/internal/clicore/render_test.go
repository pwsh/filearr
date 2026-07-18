package clicore

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/localapi"
)

func strp(s string) *string { return &s }

func sampleResp() localapi.QueryResponse {
	ext := "mkv"
	kind := "video"
	return localapi.QueryResponse{
		Rows: []localapi.ResultRow{
			{ID: "1", RelPath: "Movies/Arcane.S01E01.mkv", Filename: "Arcane.S01E01.mkv",
				Extension: &ext, Size: 1610612736, Mtime: "2026-07-15T12:00:00Z", Kind: &kind, FuzzyMatched: false},
			{ID: "2", RelPath: "Movies/Arcaen.mkv", Filename: "Arcaen.mkv",
				Extension: &ext, Size: 512, Mtime: "2026-07-17T11:30:00Z", Kind: &kind, FuzzyMatched: true},
		},
		Total: 2,
		Scope: localapi.ScopeInfo{Active: false, Predicates: []string{}},
	}
}

// TestRenderJSONShape proves --json output is one JSON object per line, snake_case
// keys straight off the wire row (jq-parseable).
func TestRenderJSONShape(t *testing.T) {
	var buf bytes.Buffer
	if err := RenderJSON(&buf, sampleResp()); err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimRight(buf.String(), "\n"), "\n")
	if len(lines) != 2 {
		t.Fatalf("expected 2 NDJSON lines, got %d: %q", len(lines), buf.String())
	}
	for i, ln := range lines {
		var row map[string]any
		if err := json.Unmarshal([]byte(ln), &row); err != nil {
			t.Fatalf("line %d not valid JSON: %v (%q)", i, err, ln)
		}
		if _, ok := row["rel_path"]; !ok {
			t.Errorf("line %d missing snake_case key rel_path: %v", i, row)
		}
		if _, ok := row["fuzzy_matched"]; !ok {
			t.Errorf("line %d missing snake_case key fuzzy_matched: %v", i, row)
		}
	}
	// The fuzzy row must carry fuzzy_matched=true.
	var second map[string]any
	json.Unmarshal([]byte(lines[1]), &second)
	if second["fuzzy_matched"] != true {
		t.Errorf("row 2 fuzzy_matched = %v; want true", second["fuzzy_matched"])
	}
}

// TestRenderPlainNoANSI proves the plain (non-color) table carries NO ANSI escape
// codes — the guarantee that piped output is clean even without --plain.
func TestRenderPlainNoANSI(t *testing.T) {
	var buf bytes.Buffer
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	if err := Render(&buf, sampleResp(), RenderOptions{Color: false, Now: now}); err != nil {
		t.Fatal(err)
	}
	out := buf.String()
	if strings.Contains(out, "\x1b[") {
		t.Fatalf("plain table contains ANSI escape codes: %q", out)
	}
	for _, want := range []string{"REL PATH", "SIZE", "MODIFIED", "STATUS", "Movies/Arcane.S01E01.mkv", "exact", "fuzzy"} {
		if !strings.Contains(out, want) {
			t.Errorf("plain table missing %q:\n%s", want, out)
		}
	}
}

// TestRenderColorHasANSI confirms the color path DOES emit ANSI (so the plain
// path's absence is meaningful, not a rendering no-op).
func TestRenderColorHasANSI(t *testing.T) {
	var buf bytes.Buffer
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	if err := Render(&buf, sampleResp(), RenderOptions{Color: true, Now: now}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(buf.String(), "\x1b[") {
		t.Fatalf("color table emitted no ANSI codes:\n%s", buf.String())
	}
}

// TestFooterRestrictedView is the R3 assertion: an active scope ALWAYS prints the
// restricted-view note; --verbose lists the predicates.
func TestFooterRestrictedView(t *testing.T) {
	resp := sampleResp()
	resp.Scope = localapi.ScopeInfo{Active: true, Predicates: []string{"rel_path GLOB 'Movies/*'", "rel_path GLOB 'TV/*'"}}

	var terse bytes.Buffer
	RenderFooter(&terse, resp, RenderOptions{Verbose: false})
	if !strings.Contains(terse.String(), "restricted view: results are path-scope filtered") {
		t.Fatalf("terse footer missing restricted-view note:\n%s", terse.String())
	}
	if strings.Contains(terse.String(), "Movies/*") {
		t.Fatalf("terse footer should NOT list predicates without --verbose:\n%s", terse.String())
	}
	if !strings.Contains(terse.String(), "2 scope predicate(s)") {
		t.Fatalf("terse footer should hint predicate count:\n%s", terse.String())
	}

	var verbose bytes.Buffer
	RenderFooter(&verbose, resp, RenderOptions{Verbose: true})
	for _, want := range []string{"restricted view", "rel_path GLOB 'Movies/*'", "rel_path GLOB 'TV/*'"} {
		if !strings.Contains(verbose.String(), want) {
			t.Errorf("verbose footer missing %q:\n%s", want, verbose.String())
		}
	}
}

// TestFooterNoticeAndTruncated covers the server notice + pagination hint surfaces.
func TestFooterNoticeAndTruncated(t *testing.T) {
	resp := sampleResp()
	resp.Notice = strp("results include local typo-tolerant (fuzzy) matches")
	resp.Truncated = true

	var buf bytes.Buffer
	RenderFooter(&buf, resp, RenderOptions{Offset: 0, Limit: 2})
	out := buf.String()
	if !strings.Contains(out, "note: results include local typo-tolerant") {
		t.Errorf("footer missing notice:\n%s", out)
	}
	if !strings.Contains(out, "use --offset 2") {
		t.Errorf("footer missing pagination hint:\n%s", out)
	}
}

// TestColorEnabledHonorsNoColor proves the color gate: color only on a TTY, off
// under --plain, and off when NO_COLOR is present and non-empty (no-color.org).
func TestColorEnabledHonorsNoColor(t *testing.T) {
	t.Setenv("NO_COLOR", "") // start from a clean, empty (= not disabling) state
	os.Unsetenv("NO_COLOR")

	if !ColorEnabled(true, false) {
		t.Error("tty + no --plain + NO_COLOR unset should enable color")
	}
	if ColorEnabled(false, false) {
		t.Error("non-tty must never enable color")
	}
	if ColorEnabled(true, true) {
		t.Error("--plain must disable color even on a tty")
	}

	t.Setenv("NO_COLOR", "1")
	if ColorEnabled(true, false) {
		t.Error("NO_COLOR=1 must disable color on a tty")
	}

	t.Setenv("NO_COLOR", "")
	if !ColorEnabled(true, false) {
		t.Error("NO_COLOR empty must NOT disable color (no-color.org: present AND non-empty)")
	}
}

func TestFormatSize(t *testing.T) {
	cases := map[int64]string{
		0:          "0 B",
		512:        "512 B",
		1024:       "1.0 KiB",
		1536:       "1.5 KiB",
		1073741824: "1.0 GiB",
	}
	for in, want := range cases {
		if got := formatSize(in); got != want {
			t.Errorf("formatSize(%d) = %q; want %q", in, got, want)
		}
	}
}

func TestHumanizeMtime(t *testing.T) {
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	cases := map[string]string{
		"2026-07-17T11:59:40Z": "just now",
		"2026-07-17T11:30:00Z": "30m ago",
		"2026-07-17T09:00:00Z": "3h ago",
		"2026-07-14T12:00:00Z": "3d ago",
	}
	for in, want := range cases {
		if got := humanizeMtime(in, now); got != want {
			t.Errorf("humanizeMtime(%q) = %q; want %q", in, got, want)
		}
	}
	if got := humanizeMtime("not-a-date", now); got != "not-a-date" {
		t.Errorf("unparseable mtime should pass through, got %q", got)
	}
}
