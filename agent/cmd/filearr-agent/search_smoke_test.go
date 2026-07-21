package main

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
)

// TestSearchCLISmoke seeds a local index under a temp data dir and drives the
// `search` subcommand end-to-end (DSL parse -> read-only execute -> NDJSON out),
// asserting the JSON row is jq-parseable and carries the DSL result shape.
func TestSearchCLISmoke(t *testing.T) {
	dir := t.TempDir()
	seedCLIIndex(t, filepath.Join(dir, indexDBName))

	out := captureStdout(t, func() {
		if err := runSearch([]string{"-data", dir, "--json", "kind:video"}); err != nil {
			t.Fatalf("runSearch: %v", err)
		}
	})

	lines := nonEmptyLines(out)
	if len(lines) != 1 {
		t.Fatalf("expected 1 NDJSON row, got %d: %q", len(lines), out)
	}
	var row map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &row); err != nil {
		t.Fatalf("NDJSON not jq-parseable: %v (%q)", err, lines[0])
	}
	if row["rel_path"] != "Movies/Film.mkv" {
		t.Errorf("unexpected rel_path: %v", row["rel_path"])
	}
	if row["fuzzy_matched"] != false {
		t.Errorf("exact hit should not be flagged fuzzy: %v", row["fuzzy_matched"])
	}

	// A malformed query surfaces a clean parse error (non-nil, no panic).
	if err := runSearch([]string{"-data", dir, "size:1X"}); err == nil {
		t.Fatal("malformed query should return an error")
	}
}

func seedCLIIndex(t *testing.T, path string) {
	t.Helper()
	st, err := index.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	ctx := context.Background()
	tx, _ := st.Begin(ctx)
	rid, err := index.EnsureRoot(ctx, tx, "/media")
	if err != nil {
		t.Fatal(err)
	}
	tx.Commit()
	now := time.Now()
	tx, _ = st.Begin(ctx)
	id, _ := index.NewID()
	it := &index.Item{ID: id, RootID: rid, RelPath: "Movies/Film.mkv", Filename: "Film.mkv",
		Extension: "mkv", Size: 10, MtimeNs: now.UnixNano(), FileCategory: "video",
		Status: index.StatusActive, FirstSeen: now, LastSeen: now}
	if err := index.InsertItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	tx.Commit()
}

func captureStdout(t *testing.T, fn func()) string {
	t.Helper()
	orig := os.Stdout
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Stdout = w
	defer func() { os.Stdout = orig }()
	fn()
	w.Close()
	buf, _ := io.ReadAll(r)
	return string(buf)
}

func nonEmptyLines(s string) []string {
	var out []string
	for _, l := range strings.Split(s, "\n") {
		if strings.TrimSpace(l) != "" {
			out = append(out, l)
		}
	}
	return out
}
