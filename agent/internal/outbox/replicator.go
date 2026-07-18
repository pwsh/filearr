package outbox

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
)

// ErrCursorDeadEnd is the sentinel a flush wraps when central expects a seq_no
// the agent cannot supply and neither a fast-forward nor a rewind moved the
// cursor (an unrecoverable replication gap). The drain keeps backing off, but a
// supervising Observer treats it as the trigger for a reset_seq full-manifest
// reconcile (P5-T5) — the designated repair path.
var ErrCursorDeadEnd = errors.New("replication cursor dead-end: central expects a seq the agent cannot supply")

// Observer receives asynchronous health signals from the drain loop so a
// supervisor (the `run` daemon) can trigger a full-manifest reconcile. Both
// methods are best-effort and must not block the drain; the implementation
// coalesces/de-dupes as it sees fit. A nil Observer disables the signals.
type Observer interface {
	// CursorDeadEnd fires when the drain hits ErrCursorDeadEnd. It fires at most
	// once per gap episode (re-armed after the next successful contact), so a
	// backing-off drain does not spam the supervisor.
	CursorDeadEnd()
	// Reconnected fires on the first successful contact that ends a continuous
	// failure streak, reporting how long the drain had been failing. A long
	// outage may have left central's view stale enough to warrant a reconcile.
	Reconnected(downFor time.Duration)
}

// Default drain triggers (research §4.3: ≈500 rows / 5s / ≈2MB, whichever first).
const (
	defaultMaxRows  = 500
	defaultMaxBytes = 2 << 20 // 2 MiB
	defaultMaxAge   = 5 * time.Second
	defaultPoll     = 1 * time.Second
	defaultTimeout  = 30 * time.Second
)

// Config configures a Replicator; zero-valued fields take the defaults above.
type Config struct {
	// BaseURL is the central root (e.g. https://filearr.example.com); a trailing
	// slash is tolerated and /api is appended internally.
	BaseURL string
	AgentID string

	// AuthFn returns the bearer token for each request. Interim scheme
	// (docs/ops/agents.md §6): the agent's cert fingerprint. Called per-request
	// so a value that changes across a run is picked up.
	AuthFn func() string

	HTTP     *http.Client
	MaxRows  int
	MaxBytes int
	MaxAge   time.Duration
	Poll     time.Duration
	Backoff  BackoffConfig
	Logger   *slog.Logger

	// Observer, if set, receives drain-health signals (cursor dead-end, reconnect
	// after an outage) so the run daemon can trigger a reconcile. Optional.
	Observer Observer

	// Clock is injectable for age-trigger tests (nil => time.Now).
	Clock func() time.Time
}

// Replicator drains the outbox to central's replication endpoint. One instance
// owns the drain loop; it never mutates items, only marks outbox rows.
type Replicator struct {
	ob       *Outbox
	baseURL  string
	agentID  string
	authFn   func() string
	http     *http.Client
	maxRows  int
	maxBytes int
	maxAge   time.Duration
	poll     time.Duration
	backoff  *Backoff
	log      *slog.Logger
	clock    func() time.Time
	observer Observer
}

// NewReplicator wires a Replicator over ob.
func NewReplicator(ob *Outbox, cfg Config) *Replicator {
	r := &Replicator{
		ob:       ob,
		baseURL:  strings.TrimRight(cfg.BaseURL, "/"),
		agentID:  cfg.AgentID,
		authFn:   cfg.AuthFn,
		http:     cfg.HTTP,
		maxRows:  cfg.MaxRows,
		maxBytes: cfg.MaxBytes,
		maxAge:   cfg.MaxAge,
		poll:     cfg.Poll,
		backoff:  NewBackoff(cfg.Backoff),
		log:      cfg.Logger,
		clock:    cfg.Clock,
		observer: cfg.Observer,
	}
	if r.http == nil {
		r.http = &http.Client{Timeout: defaultTimeout}
	}
	if r.maxRows <= 0 {
		r.maxRows = defaultMaxRows
	}
	if r.maxBytes <= 0 {
		r.maxBytes = defaultMaxBytes
	}
	if r.maxAge <= 0 {
		r.maxAge = defaultMaxAge
	}
	if r.poll <= 0 {
		r.poll = defaultPoll
	}
	if r.log == nil {
		r.log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if r.clock == nil {
		r.clock = time.Now
	}
	if r.authFn == nil {
		r.authFn = func() string { return "" }
	}
	return r
}

// Counters accumulate the outcome of a drain (a Push, or one Run flush).
type Counters struct {
	Batches    int   // batches POSTed and accepted (200)
	Rows       int   // outbox rows marked sent (applied + fast-forwarded)
	Applied    int   // central-reported applied
	Upserted   int   // central-reported upserted
	Tombstoned int   // central-reported tombstoned
	LastSeq    int64 // central's highest contiguous seq after the last 200
}

func (c *Counters) add(o Counters) {
	c.Batches += o.Batches
	c.Rows += o.Rows
	c.Applied += o.Applied
	c.Upserted += o.Upserted
	c.Tombstoned += o.Tombstoned
	if o.LastSeq > c.LastSeq {
		c.LastSeq = o.LastSeq
	}
}

// applyResponse is central's 200 body (backend replication-batch endpoint).
type applyResponse struct {
	Applied          int   `json:"applied"`
	Upserted         int   `json:"upserted"`
	Tombstoned       int   `json:"tombstoned"`
	NoopTombstones   int   `json:"noop_tombstones"`
	LibrariesCreated int   `json:"libraries_created"`
	LastSeq          int64 `json:"last_seq"`
}

// conflictResponse is central's 409 body: rewind/fast-forward to expected_seq_no.
type conflictResponse struct {
	Reason        string `json:"reason"`
	ExpectedSeqNo int64  `json:"expected_seq_no"`
}

// retryError marks a flush failure the drain should back off and retry (network
// down, 5xx, 401/403/404/413, or an unrecoverable 409 gap). A nil error from
// flush means progress was made (a 200 ACK or a cursor-moving 409).
type retryError struct{ err error }

func (e *retryError) Error() string { return e.err.Error() }
func (e *retryError) Unwrap() error { return e.err }

// Run drains the outbox until ctx is cancelled, honouring the count/age/size
// triggers and backing off on failure (block-don't-drop: unsent rows accumulate,
// never dropped). It returns ctx.Err() on shutdown. Between-batch shutdown is
// clean; a shutdown mid-flight either completes the mark (detached ctx) or leaves
// the batch durably unsent for a resend (central 409-dedupes the replay).
func (r *Replicator) Run(ctx context.Context) error {
	// Failure-streak tracking for the reconnect signal (trigger b): firstFailure
	// is the start of the current continuous outage (zero when healthy);
	// deadEndArmed de-dupes the cursor-dead-end signal to once per gap episode.
	var firstFailure time.Time
	deadEndArmed := true
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		rows, err := r.ob.Unsent(ctx, r.maxRows)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if !sleepCtx(ctx, r.backoff.Next()) {
				return ctx.Err()
			}
			continue
		}
		if len(rows) == 0 {
			if !sleepCtx(ctx, r.poll) {
				return ctx.Err()
			}
			continue
		}
		oldestAge := r.clock().Sub(rows[0].WrittenAt)
		if len(rows) < r.maxRows && payloadBytes(rows) < r.maxBytes && oldestAge < r.maxAge {
			// Not enough to flush yet: wait out the remaining age window, then
			// re-read (more rows may have accrued) and re-evaluate.
			wait := r.maxAge - oldestAge
			if wait > r.poll {
				wait = r.poll
			}
			if !sleepCtx(ctx, wait) {
				return ctx.Err()
			}
			continue
		}
		if _, err := r.flush(ctx, rows); err != nil {
			if errors.Is(err, ErrCursorDeadEnd) && deadEndArmed && r.observer != nil {
				deadEndArmed = false
				r.observer.CursorDeadEnd()
			}
			if firstFailure.IsZero() {
				firstFailure = r.clock()
			}
			d := r.backoff.Next()
			r.log.Warn("replication flush failed; backing off", "backoff", d.String(), "err", err)
			if !sleepCtx(ctx, d) {
				return ctx.Err()
			}
			continue
		}
		// Successful contact: end any outage streak and re-arm the dead-end signal.
		if !firstFailure.IsZero() {
			downFor := r.clock().Sub(firstFailure)
			firstFailure = time.Time{}
			if r.observer != nil {
				r.observer.Reconnected(downFor)
			}
		}
		deadEndArmed = true
		r.backoff.Reset()
	}
}

// Push drains until the outbox is empty or a flush errors, ignoring the age gate
// (flush-now). It is the one-shot `filearr-agent push` path and the test drain.
func (r *Replicator) Push(ctx context.Context) (Counters, error) {
	var total Counters
	for {
		if err := ctx.Err(); err != nil {
			return total, err
		}
		rows, err := r.ob.Unsent(ctx, r.maxRows)
		if err != nil {
			return total, err
		}
		if len(rows) == 0 {
			return total, nil
		}
		c, err := r.flush(ctx, rows)
		total.add(c)
		if err != nil {
			return total, err
		}
	}
}

// flush sends ONE batch built from the head of rows (capped at maxBytes, always
// ≥1 row) and applies the response. A returned error is a *retryError; nil means
// the cursor advanced (200 ACK or a cursor-moving 409).
func (r *Replicator) flush(ctx context.Context, rows []Row) (Counters, error) {
	batch, evs, err := r.assemble(rows)
	if err != nil {
		return Counters{}, &retryError{err}
	}
	firstSeq, lastSeq := evs[0].SeqNo, evs[len(evs)-1].SeqNo
	batchID := newBatchID()

	status, body, err := r.post(ctx, batch)
	if err != nil {
		return Counters{}, &retryError{fmt.Errorf("post batch [%d,%d]: %w", firstSeq, lastSeq, err)}
	}

	switch status {
	case http.StatusOK:
		var ar applyResponse
		if err := json.Unmarshal(body, &ar); err != nil {
			return Counters{}, &retryError{fmt.Errorf("decode 200 body: %w", err)}
		}
		// Detached ctx: a graceful shutdown right after the ACK must still persist
		// the mark, else the batch replays needlessly on restart.
		n, err := r.ob.MarkSent(context.WithoutCancel(ctx), firstSeq, lastSeq, batchID)
		if err != nil {
			return Counters{}, &retryError{err}
		}
		r.log.Info("replication batch accepted",
			"batch_id", batchID, "from", firstSeq, "to", lastSeq,
			"marked", n, "applied", ar.Applied, "last_seq", ar.LastSeq)
		return Counters{
			Batches: 1, Rows: int(n), Applied: ar.Applied,
			Upserted: ar.Upserted, Tombstoned: ar.Tombstoned, LastSeq: ar.LastSeq,
		}, nil

	case http.StatusConflict:
		var cr conflictResponse
		if err := json.Unmarshal(body, &cr); err != nil {
			return Counters{}, &retryError{fmt.Errorf("decode 409 body: %w", err)}
		}
		fwd, rwd, err := r.ob.SetCursor(context.WithoutCancel(ctx), cr.ExpectedSeqNo, newBatchID())
		if err != nil {
			return Counters{}, &retryError{err}
		}
		if fwd == 0 && rwd == 0 {
			// Cursor did not move: central expects seq below our lowest unsent row
			// and we hold nothing there — an unrecoverable gap. Back off; a
			// full-manifest reconcile (P5-T5) is the repair path, not a hot loop.
			// Wrap ErrCursorDeadEnd so Run/Push can route it to a reconcile.
			return Counters{}, &retryError{fmt.Errorf(
				"replication gap: central expects seq %d (reason %q) the agent cannot supply; awaiting reconcile: %w",
				cr.ExpectedSeqNo, cr.Reason, ErrCursorDeadEnd)}
		}
		r.log.Info("replication cursor reset from 409",
			"reason", cr.Reason, "expected", cr.ExpectedSeqNo, "fast_forwarded", fwd, "rewound", rwd)
		return Counters{Rows: int(fwd)}, nil

	default:
		return Counters{}, &retryError{r.statusError(status, body)}
	}
}

// assemble builds a ReplicationBatch from the head of rows, capping the total
// payload at maxBytes (but always including at least one row so progress is
// possible even for an oversized single event).
func (r *Replicator) assemble(rows []Row) (replicationBatch, []wireEvent, error) {
	evs := make([]wireEvent, 0, len(rows))
	size := 0
	for i, row := range rows {
		var we wireEvent
		if err := json.Unmarshal([]byte(row.Payload), &we); err != nil {
			return replicationBatch{}, nil, fmt.Errorf("decode outbox payload seq %d: %w", row.SeqNo, err)
		}
		we.SeqNo = row.SeqNo // the stored payload carries no seq_no; the column is authoritative
		evLen := len(row.Payload)
		if i > 0 && size+evLen > r.maxBytes {
			break
		}
		evs = append(evs, we)
		size += evLen
	}
	return replicationBatch{AgentID: r.agentID, Entries: evs}, evs, nil
}

// replicationBatch is the POST body (backend ReplicationBatch).
type replicationBatch struct {
	AgentID string      `json:"agent_id"`
	Entries []wireEvent `json:"entries"`
}

func (r *Replicator) post(ctx context.Context, batch replicationBatch) (int, []byte, error) {
	buf, err := json.Marshal(batch)
	if err != nil {
		return 0, nil, fmt.Errorf("marshal batch: %w", err)
	}
	url := fmt.Sprintf("%s/api/v1/agents/%s/replication-batch", r.baseURL, r.agentID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(buf))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if tok := r.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	resp, err := r.http.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return resp.StatusCode, body, nil
}

// statusError renders a clear, actionable message per terminal-ish status. All
// are retried with backoff (central may be reconfigured/re-enabled), but 404
// (feature-off) is called out explicitly so an operator is not left guessing.
func (r *Replicator) statusError(status int, body []byte) error {
	detail := centralDetail(body)
	switch status {
	case http.StatusNotFound:
		return fmt.Errorf("central replication endpoint returned 404 — the replication feature appears disabled on this server (will retry): %s", detail)
	case http.StatusUnauthorized, http.StatusForbidden:
		return fmt.Errorf("central rejected the agent bearer token (%d) — check the bound cert fingerprint / FILEARR_AGENT_AUTH_FINGERPRINT: %s", status, detail)
	case http.StatusRequestEntityTooLarge:
		return fmt.Errorf("central rejected the batch as too large (413) — lower the drain byte cap: %s", detail)
	default:
		return fmt.Errorf("central returned %d: %s", status, detail)
	}
}

// centralDetail unwraps a FastAPI {"detail": "..."} envelope for logging.
func centralDetail(body []byte) string {
	var env struct {
		Detail string `json:"detail"`
	}
	if json.Unmarshal(body, &env) == nil && env.Detail != "" {
		return env.Detail
	}
	if len(body) > 512 {
		body = body[:512]
	}
	return string(body)
}

// payloadBytes sums the stored payload sizes (a close proxy for the wire size).
func payloadBytes(rows []Row) int {
	n := 0
	for _, r := range rows {
		n += len(r.Payload)
	}
	return n
}

// newBatchID returns a time-ordered UUIDv7 string — sortable and traceable in
// both the outbox.batch_id column and central's replication ledger.
func newBatchID() string {
	if id, err := uuid.NewV7(); err == nil {
		return id.String()
	}
	return fmt.Sprintf("batch-%d", time.Now().UnixNano())
}

// sleepCtx sleeps for d or until ctx is cancelled; false => cancelled first.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		select {
		case <-ctx.Done():
			return false
		default:
			return true
		}
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
