package commands

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"

	"golang.org/x/time/rate"
)

// StagingRef is the transfer state central reports on attach / append: the
// resume anchor (BytesTransferred == the committed offset) plus the lifecycle
// state and declared size.
type StagingRef struct {
	ID               string
	State            string
	BytesTransferred int64
	TotalBytes       int64
}

// StageResult is the executor's outcome for a stage_upload, carried back in the
// command's complete result (transfer id + total bytes acked).
type StageResult struct {
	TransferID string
	TotalBytes int64
}

// OffsetMismatchError is returned by Uploader.Append when central rejects a chunk
// because Upload-Offset != the committed offset (tus 409 discipline). It carries
// central's authoritative offset so the executor re-seeks and retries — the
// idempotent recovery under the at-least-once command queue.
type OffsetMismatchError struct{ Offset int64 }

func (e *OffsetMismatchError) Error() string {
	return fmt.Sprintf("staging offset mismatch: central committed offset is %d", e.Offset)
}

// Uploader is the central staging data-plane the executor drives (the hand-rolled
// tus-subset client, docs/research/phase-10-t4-transport-spike.md). The Poller
// implements it over the shared bearer/mTLS HTTP client; tests inject a fake
// in-memory staging server to exercise resume / rate / traversal without a network.
type Uploader interface {
	// Attach creates-or-returns the transfer row for commandID (idempotent per
	// command; a restarted agent re-attaches the SAME row) and returns its current
	// state, including the committed resume offset.
	Attach(ctx context.Context, commandID string, totalBytes int64) (StagingRef, error)
	// Append writes chunk at offset and returns the new committed state. A wrong
	// offset surfaces as *OffsetMismatchError carrying central's committed offset.
	Append(ctx context.Context, transferID string, offset int64, chunk []byte) (StagingRef, error)
}

// StageUpload streams the file named by the command's {library_ref, rel_path}
// payload to central's staging area, resumably and rate-limited.
//
// Order is load-bearing for security (research §4, defense in depth): the path is
// re-validated against the agent's OWN configured roots BEFORE the file is ever
// opened — an out-of-root/traversal path returns an error with NO stat and NO
// open, so a confused/compromised central can never make the agent read outside a
// root. The resume point is ALWAYS central's committed offset (from Attach): the
// agent keeps NO local upload state, so a restart mid-transfer continues from
// exactly where central last durably acked. rateBytesPerSec (0 = unlimited) sizes
// a token bucket applied to every chunk BEFORE it is sent.
//
// stageMu enforces the 1-upload/agent cap explicitly: a second StageUpload blocks
// until the first returns.
func (e *Executor) StageUpload(
	ctx context.Context,
	commandID string,
	raw map[string]any,
	up Uploader,
	rateBytesPerSec int64,
) (StageResult, error) {
	e.stageMu.Lock()
	defer e.stageMu.Unlock()

	p, err := decodePayload(raw)
	if err != nil {
		return StageResult{}, err
	}
	// Re-validate root membership + traversal BEFORE any read (no stat, no open on
	// an out-of-root path).
	full, err := e.resolve(ctx, p.LibraryRef, p.RelPath)
	if err != nil {
		return StageResult{}, err
	}

	fi, err := os.Stat(full)
	if err != nil {
		return StageResult{}, fmt.Errorf("stat %s: %w", p.RelPath, err)
	}
	if fi.IsDir() {
		return StageResult{}, fmt.Errorf("%s is a directory, not a file", p.RelPath)
	}
	size := fi.Size()

	ref, err := up.Attach(ctx, commandID, size)
	if err != nil {
		return StageResult{}, fmt.Errorf("attach transfer: %w", err)
	}
	// Already fully staged (a restart AFTER completion, or an earlier run finished
	// before the command was re-completed): nothing to send.
	if ref.State == "staged" || ref.State == "downloaded" {
		return StageResult{TransferID: ref.ID, TotalBytes: size}, nil
	}

	f, err := os.Open(full)
	if err != nil {
		return StageResult{}, fmt.Errorf("open %s: %w", p.RelPath, err)
	}
	defer f.Close()

	chunk := e.chunkBytes
	if chunk <= 0 {
		chunk = DefaultChunkBytes
	}
	var lim *rate.Limiter
	if rateBytesPerSec > 0 {
		burst := chunk
		if int64(burst) < rateBytesPerSec {
			burst = int(rateBytesPerSec) // burst >= the largest WaitN(n) we pass
		}
		lim = rate.NewLimiter(rate.Limit(rateBytesPerSec), burst)
	}

	offset := ref.BytesTransferred
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return StageResult{}, fmt.Errorf("seek to resume offset %d: %w", offset, err)
	}
	buf := make([]byte, chunk)
	for offset < size {
		want := int64(chunk)
		if rem := size - offset; rem < want {
			want = rem
		}
		n, rerr := io.ReadFull(f, buf[:want])
		if rerr != nil && !errors.Is(rerr, io.ErrUnexpectedEOF) {
			return StageResult{}, fmt.Errorf("read %s at %d: %w", p.RelPath, offset, rerr)
		}
		if n == 0 {
			return StageResult{}, fmt.Errorf("short read of %s at %d", p.RelPath, offset)
		}
		if lim != nil {
			if err := lim.WaitN(ctx, n); err != nil {
				return StageResult{}, fmt.Errorf("rate limit wait: %w", err)
			}
		}
		next, err := up.Append(ctx, ref.ID, offset, buf[:n])
		if err != nil {
			var mm *OffsetMismatchError
			if errors.As(err, &mm) {
				// Central's offset diverged (a lost ack / redelivered command):
				// re-seek to its committed offset and continue — idempotent.
				offset = mm.Offset
				if _, serr := f.Seek(offset, io.SeekStart); serr != nil {
					return StageResult{}, fmt.Errorf("re-seek to %d: %w", offset, serr)
				}
				continue
			}
			return StageResult{}, fmt.Errorf("append at %d: %w", offset, err)
		}
		ref = next
		offset = next.BytesTransferred
	}

	// Empty file (or a completion whose staged transition central still owes): a
	// single zero-length append at offset==size flips central to `staged`.
	if ref.State != "staged" {
		next, err := up.Append(ctx, ref.ID, offset, nil)
		if err != nil {
			return StageResult{}, fmt.Errorf("finalize at %d: %w", offset, err)
		}
		ref = next
	}
	return StageResult{TransferID: ref.ID, TotalBytes: size}, nil
}
