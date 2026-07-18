package commands

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeTransfer is one in-memory central staging row.
type fakeTransfer struct {
	id        string
	commandID string
	total     int64
	committed int64
	buf       []byte
	state     string
}

func (t *fakeTransfer) ref() StagingRef {
	return StagingRef{ID: t.id, State: t.state, BytesTransferred: t.committed, TotalBytes: t.total}
}

// fakeCentral is an in-memory model of central's staging plane: idempotent attach
// per command, offset-checked append with the tus 409 discipline, a durable
// committed prefix. Knobs simulate an interrupt (failAt), a lost-ack offset
// divergence (mismatchOnce), rate/serialisation timing (appendSleep + overlap).
type fakeCentral struct {
	mu          sync.Mutex
	transfers   map[string]*fakeTransfer
	byCommand   map[string]string
	seq         int
	attachCalls int
	appendCount int

	failAt       int  // >0: Append errors AFTER this many successful appends
	mismatchOnce bool // apply the write then 409 with the new offset, once

	appendSleep time.Duration
	active      atomic.Int32
	overlap     atomic.Bool
}

func newFakeCentral() *fakeCentral {
	return &fakeCentral{
		transfers: map[string]*fakeTransfer{},
		byCommand: map[string]string{},
	}
}

func (c *fakeCentral) Attach(_ context.Context, commandID string, total int64) (StagingRef, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.attachCalls++
	if tid, ok := c.byCommand[commandID]; ok {
		return c.transfers[tid].ref(), nil // idempotent re-attach
	}
	c.seq++
	tid := fmt.Sprintf("t%d", c.seq)
	t := &fakeTransfer{id: tid, commandID: commandID, total: total, state: "pending"}
	c.transfers[tid] = t
	c.byCommand[commandID] = tid
	return t.ref(), nil
}

func (c *fakeCentral) Append(_ context.Context, tid string, offset int64, chunk []byte) (StagingRef, error) {
	if n := c.active.Add(1); n > 1 {
		c.overlap.Store(true)
	}
	defer c.active.Add(-1)
	if c.appendSleep > 0 {
		time.Sleep(c.appendSleep)
	}

	c.mu.Lock()
	defer c.mu.Unlock()
	t := c.transfers[tid]
	if t == nil {
		return StagingRef{}, errors.New("no such transfer")
	}
	c.appendCount++
	if c.failAt > 0 && c.appendCount > c.failAt {
		return StagingRef{}, errors.New("simulated network drop mid-transfer")
	}
	if offset != t.committed {
		return StagingRef{}, &OffsetMismatchError{Offset: t.committed}
	}
	if t.total >= 0 && offset+int64(len(chunk)) > t.total {
		return StagingRef{}, errors.New("chunk would exceed total_bytes")
	}
	// Commit: truncate to offset (discard any un-acked tail) then append.
	t.buf = append(t.buf[:offset], chunk...)
	t.committed = offset + int64(len(chunk))
	if t.state == "pending" {
		t.state = "uploading"
	}
	if t.committed >= t.total {
		t.state = "staged"
	}
	if c.mismatchOnce {
		// The write landed but the ack is "lost": tell the agent its offset is
		// stale and hand back the true committed offset (idempotent recovery).
		c.mismatchOnce = false
		return StagingRef{}, &OffsetMismatchError{Offset: t.committed}
	}
	return t.ref(), nil
}

func (c *fakeCentral) staged(tid string) *fakeTransfer {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.transfers[tid]
}

func stageExec(root string, chunkBytes int) *Executor {
	ex := NewExecutor(fakeRoots{[]string{root}}, 0)
	ex.chunkBytes = chunkBytes
	return ex
}

func TestStageUploadHappyPathMultiChunk(t *testing.T) {
	root := t.TempDir()
	data := bytes.Repeat([]byte("filearr-"), 5000) // 40 KB
	writeFile(t, root, "media/clip.mkv", data)
	ex := stageExec(root, 4096)
	central := newFakeCentral()

	res, err := ex.StageUpload(context.Background(), "cmd-1", map[string]any{
		"library_ref": root, "rel_path": "media/clip.mkv",
	}, central, 0)
	if err != nil {
		t.Fatalf("stage: %v", err)
	}
	if res.TotalBytes != int64(len(data)) {
		t.Fatalf("total bytes: got %d want %d", res.TotalBytes, len(data))
	}
	got := central.staged(res.TransferID)
	if got.state != "staged" {
		t.Fatalf("state: got %q want staged", got.state)
	}
	if !bytes.Equal(got.buf, data) {
		t.Fatalf("staged bytes differ from source")
	}
}

// THE accept criterion: an interrupted multi-chunk upload resumes from the last
// committed offset and the staged file's sha256 equals the source's.
func TestStageUploadResumeAfterInterrupt(t *testing.T) {
	root := t.TempDir()
	data := make([]byte, 33_000)
	if _, err := rand.Read(data); err != nil {
		t.Fatal(err)
	}
	writeFile(t, root, "big.bin", data)
	central := newFakeCentral()
	central.failAt = 2 // die after 2 committed chunks

	// Run 1: uploads 2 chunks then aborts.
	ex1 := stageExec(root, 4096)
	_, err := ex1.StageUpload(context.Background(), "cmd-x", map[string]any{
		"library_ref": root, "rel_path": "big.bin",
	}, central, 0)
	if err == nil {
		t.Fatal("run 1 was expected to fail mid-transfer")
	}
	central.mu.Lock()
	committed := central.transfers["t1"].committed
	central.mu.Unlock()
	if committed == 0 || committed >= int64(len(data)) {
		t.Fatalf("run 1 should have partially committed, got %d", committed)
	}

	// Run 2: a fresh executor (no local upload state) re-attaches and resumes from
	// central's committed offset.
	central.failAt = 0
	ex2 := stageExec(root, 4096)
	res, err := ex2.StageUpload(context.Background(), "cmd-x", map[string]any{
		"library_ref": root, "rel_path": "big.bin",
	}, central, 0)
	if err != nil {
		t.Fatalf("run 2 resume: %v", err)
	}
	if central.attachCalls < 2 {
		t.Fatalf("expected a re-attach on run 2 (attachCalls=%d)", central.attachCalls)
	}
	got := central.staged(res.TransferID)
	if got.state != "staged" {
		t.Fatalf("final state: %q", got.state)
	}
	if sha256.Sum256(got.buf) != sha256.Sum256(data) {
		t.Fatalf("resumed staged sha256 != source sha256")
	}
}

// A wrong Upload-Offset (simulated lost-ack divergence) is recovered by re-seeking
// to central's committed offset — idempotent under the at-least-once queue.
func TestStageUploadOffsetMismatchRecovery(t *testing.T) {
	root := t.TempDir()
	data := bytes.Repeat([]byte("ABCD"), 3000) // 12 KB
	writeFile(t, root, "m.dat", data)
	central := newFakeCentral()
	central.mismatchOnce = true
	ex := stageExec(root, 4096)

	res, err := ex.StageUpload(context.Background(), "cmd-r", map[string]any{
		"library_ref": root, "rel_path": "m.dat",
	}, central, 0)
	if err != nil {
		t.Fatalf("mismatch recovery: %v", err)
	}
	got := central.staged(res.TransferID)
	if got.state != "staged" || !bytes.Equal(got.buf, data) {
		t.Fatalf("recovery produced wrong bytes/state: state=%q equal=%v", got.state, bytes.Equal(got.buf, data))
	}
}

// An out-of-root rel_path is refused BEFORE any read: the file is never opened and
// central is never even attached.
func TestStageUploadOutOfRootNeverReads(t *testing.T) {
	root := t.TempDir()
	// A sentinel file OUTSIDE the root whose open we would detect via central attach.
	outside := filepath.Join(t.TempDir(), "secret.bin")
	if err := os.WriteFile(outside, []byte("do not read me"), 0o600); err != nil {
		t.Fatal(err)
	}
	central := newFakeCentral()
	ex := stageExec(root, 4096)

	_, err := ex.StageUpload(context.Background(), "cmd-esc", map[string]any{
		"library_ref": root, "rel_path": "../../../../../../etc/passwd",
	}, central, 0)
	if err == nil {
		t.Fatal("expected refusal for an out-of-root rel_path")
	}
	if central.attachCalls != 0 || central.appendCount != 0 {
		t.Fatalf("out-of-root path must not touch central (attach=%d append=%d)",
			central.attachCalls, central.appendCount)
	}
}

// Rate limiting measurably caps throughput (small bucket + larger payload).
func TestStageUploadRateLimited(t *testing.T) {
	root := t.TempDir()
	data := make([]byte, 300_000)
	writeFile(t, root, "r.bin", data)
	central := newFakeCentral()
	ex := stageExec(root, 50_000)

	start := time.Now()
	_, err := ex.StageUpload(context.Background(), "cmd-rl", map[string]any{
		"library_ref": root, "rel_path": "r.bin",
	}, central, 100_000) // 100 KB/s
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("rate-limited stage: %v", err)
	}
	// 300 KB at 100 KB/s with a 50 KB burst => ~2.5s of throttle. Floor generously
	// at 1.2s (well above the unthrottled ~ms) and cap at 5s to keep the test fast.
	if elapsed < 1200*time.Millisecond {
		t.Fatalf("rate limiting did not slow the upload (elapsed %s)", elapsed)
	}
	if elapsed > 5*time.Second {
		t.Fatalf("rate-limited upload too slow for the test budget (elapsed %s)", elapsed)
	}
}

// Two stage_uploads never overlap: stageMu enforces the 1-upload/agent cap.
func TestStageUploadSerial(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "a.bin", bytes.Repeat([]byte("a"), 20_000))
	writeFile(t, root, "b.bin", bytes.Repeat([]byte("b"), 20_000))
	central := newFakeCentral()
	central.appendSleep = 2 * time.Millisecond
	ex := stageExec(root, 4096) // several appends each

	var wg sync.WaitGroup
	for _, f := range []struct{ cmd, rel string }{{"c-a", "a.bin"}, {"c-b", "b.bin"}} {
		wg.Add(1)
		go func(cmd, rel string) {
			defer wg.Done()
			if _, err := ex.StageUpload(context.Background(), cmd, map[string]any{
				"library_ref": root, "rel_path": rel,
			}, central, 0); err != nil {
				t.Errorf("stage %s: %v", rel, err)
			}
		}(f.cmd, f.rel)
	}
	wg.Wait()
	if central.overlap.Load() {
		t.Fatal("two stage_uploads ran concurrently — the 1-upload/agent cap failed")
	}
}

func TestStageUploadEmptyFile(t *testing.T) {
	root := t.TempDir()
	writeFile(t, root, "empty.bin", nil)
	central := newFakeCentral()
	ex := stageExec(root, 4096)

	res, err := ex.StageUpload(context.Background(), "cmd-0", map[string]any{
		"library_ref": root, "rel_path": "empty.bin",
	}, central, 0)
	if err != nil {
		t.Fatalf("empty stage: %v", err)
	}
	got := central.staged(res.TransferID)
	if got.state != "staged" || len(got.buf) != 0 {
		t.Fatalf("empty file: state=%q len=%d", got.state, len(got.buf))
	}
}
