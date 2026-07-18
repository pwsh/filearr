// Package commands is the agent-side consumer of central's on-demand
// “agent_commands“ queue (P10-T1/T3): a Poller long-... plain-polls the
// per-agent command channel, executes each stat_check / rehash_check against
// LOCAL DISK (never the index cache — verifying freshness is the whole point),
// and reports a CommandResult back. It reuses the shared bearer-auth + mTLS HTTP
// seam the replicator/reconcile clients use.
//
// The wire contract mirrors backend/filearr/api/agent_commands.py exactly:
//   - poll:     POST /api/v1/agents/{id}/commands/poll        {"max": N}     -> [CommandOut]
//   - ack:      POST /api/v1/agents/{id}/commands/{cid}/ack                  -> CommandOut
//   - complete: POST /api/v1/agents/{id}/commands/{cid}/complete {ok,result} -> CommandOut
//
// and the payload / result shapes mirror backend/filearr/transfers.py
// (AgentCommand payload + CommandResult).
package commands

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/scan"
)

// Command kinds (mirror backend agent_commands CHECK vocabulary).
const (
	KindStatCheck   = "stat_check"
	KindRehashCheck = "rehash_check"
	KindStageUpload = "stage_upload"
)

// payload is the JSON shape central enqueues on an agent_commands row (P10-T3
// contract): the agent resolves library_ref -> its local root, then rel_path
// under it. Content is true only for a rehash_check that wants the full hash.
type payload struct {
	LibraryRef string `json:"library_ref"`
	RelPath    string `json:"rel_path"`
	Content    bool   `json:"content"`
}

// CommandResult is the agent's report for a picked-up command — byte-compatible
// with backend/filearr/transfers.py:CommandResult (the central-side validation
// contract). Pointer fields marshal to JSON null when unset (== Python None), so
// a stat_check reports only exists/size/mtime and a skipped/absent hash is an
// unambiguous null paired with ContentSkipped.
type CommandResult struct {
	Exists         bool     `json:"exists"`
	Size           *int64   `json:"size"`
	Mtime          *float64 `json:"mtime"`
	QuickHash      *string  `json:"quick_hash"`
	ContentHash    *string  `json:"content_hash"`
	ContentSkipped bool     `json:"content_skipped"`
}

// RootLister resolves the agent's configured scan roots. *index.Store satisfies
// it; tests inject a fake so the executor needs no full SQLite store.
type RootLister interface {
	Roots(ctx context.Context) ([]index.RootRef, error)
}

// Executor runs one verification command against local disk. quickHash/fullHash
// are injectable (default: the shared tiered scan helpers) so a test can drive a
// deliberately-slow content hash for the lease-heartbeat path.
type Executor struct {
	roots        RootLister
	fullMaxBytes int64
	quickHash    func(pathStr string, size int64) (string, error)
	fullHash     func(pathStr string) (string, error)

	// chunkBytes is the stage_upload PATCH chunk size (default DefaultChunkBytes);
	// tests shrink it to exercise multi-chunk resume/rate paths.
	chunkBytes int
	// stageMu enforces the 1-upload/agent concurrency cap EXPLICITLY (research
	// §2.4): even though the Poller drains a poll batch sequentially, a second
	// stage_upload can never overlap the first. A test drives two concurrent
	// StageUpload calls and asserts they serialise.
	stageMu sync.Mutex
}

// DefaultChunkBytes is the stage_upload PATCH chunk size: 8 MiB — large enough to
// amortise per-request overhead on a multi-GB video, small enough that a resume
// re-sends at most one chunk (research §2.2).
const DefaultChunkBytes = 8 << 20

// NewExecutor wires an Executor. A non-positive fullMaxBytes takes the central
// default ceiling (scan.DefaultFullMaxBytes).
func NewExecutor(roots RootLister, fullMaxBytes int64) *Executor {
	if fullMaxBytes <= 0 {
		fullMaxBytes = scan.DefaultFullMaxBytes
	}
	return &Executor{
		roots:        roots,
		fullMaxBytes: fullMaxBytes,
		quickHash:    scan.QuickHash,
		fullHash:     scan.FullHash,
		chunkBytes:   DefaultChunkBytes,
	}
}

// Execute runs one command and returns its CommandResult.
//
// A returned error is an "I cannot answer this" condition (unknown library_ref,
// a rel_path that escapes the root, or a non-not-exist stat error): the caller
// completes the command ok=false with the error note, and central does NOT
// reconcile it (never a false tombstone). A definitively-absent file is NOT an
// error — it returns exists=false (ok=true), the signal that DOES tombstone.
func (e *Executor) Execute(ctx context.Context, kind string, raw map[string]any) (CommandResult, error) {
	p, err := decodePayload(raw)
	if err != nil {
		return CommandResult{}, err
	}
	full, err := e.resolve(ctx, p.LibraryRef, p.RelPath)
	if err != nil {
		return CommandResult{}, err
	}

	// content is only ever requested on a rehash_check (never a stat).
	wantContent := kind == KindRehashCheck && p.Content

	fi, statErr := os.Stat(full)
	if statErr != nil {
		if os.IsNotExist(statErr) {
			// Definitive answer: the file is gone. exists=false drives the
			// invariant-4 tombstone. content was "requested but not computed"
			// on a rehash, so flag it skipped for an unambiguous null hash.
			return CommandResult{Exists: false, ContentSkipped: wantContent}, nil
		}
		// Ambiguous (permission denied, IO error): refuse rather than risk a
		// false tombstone.
		return CommandResult{}, fmt.Errorf("stat %s: %w", p.RelPath, statErr)
	}

	size := fi.Size()
	mtime := float64(fi.ModTime().UnixNano()) / 1e9
	res := CommandResult{Exists: true, Size: &size, Mtime: &mtime}

	if kind != KindRehashCheck {
		// stat_check: existence + size/mtime only, no hashing.
		return res, nil
	}

	// rehash_check: quick_hash ALWAYS (bounded ~128 KiB read), from DISK.
	if q, err := e.quickHash(full, size); err == nil {
		res.QuickHash = &q
	}
	// content_hash only when requested AND within the local size ceiling; an
	// oversize (or un-requested) content hash is skipped, flagged so a null
	// content_hash is never misread as "no content".
	if wantContent && size <= e.fullMaxBytes {
		if c, err := e.fullHash(full); err == nil {
			res.ContentHash = &c
		} else {
			res.ContentSkipped = true
		}
	} else if wantContent {
		res.ContentSkipped = true
	}
	return res, nil
}

// resolve validates library_ref against the configured roots and joins rel_path
// under it, refusing any path that would escape the root (defense in depth,
// research §4 — the agent never reads outside a configured root on command).
func (e *Executor) resolve(ctx context.Context, libraryRef, relPath string) (string, error) {
	roots, err := e.roots.Roots(ctx)
	if err != nil {
		return "", fmt.Errorf("list roots: %w", err)
	}
	known := false
	for _, r := range roots {
		if r.Path == libraryRef {
			known = true
			break
		}
	}
	if !known {
		return "", fmt.Errorf("unknown library_ref %q (not a configured root)", libraryRef)
	}
	full := filepath.Join(libraryRef, filepath.FromSlash(relPath))
	rel, err := filepath.Rel(libraryRef, full)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("rel_path %q escapes root", relPath)
	}
	return full, nil
}

// decodePayload narrows the JSONB payload map into the typed payload, requiring a
// non-empty library_ref + rel_path.
func decodePayload(raw map[string]any) (payload, error) {
	var p payload
	if v, ok := raw["library_ref"].(string); ok {
		p.LibraryRef = v
	}
	if v, ok := raw["rel_path"].(string); ok {
		p.RelPath = v
	}
	if v, ok := raw["content"].(bool); ok {
		p.Content = v
	}
	if p.LibraryRef == "" || p.RelPath == "" {
		return payload{}, fmt.Errorf("payload missing library_ref/rel_path")
	}
	return p, nil
}
