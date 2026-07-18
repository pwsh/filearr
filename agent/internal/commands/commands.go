package commands

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math/rand"
	"net/http"
	"strings"
	"time"
)

// Defaults (mirroring the central caps in config.py). The poll cap is clamped
// server-side to FILEARR_AGENT_COMMAND_POLL_MAX (50); the lease default matches
// FILEARR_AGENT_COMMAND_LEASE_SECONDS (300s).
const (
	defaultMaxCommands  = 10
	defaultInterval     = 60 * time.Second
	defaultLeaseSeconds = 300
	defaultTimeout      = 60 * time.Second
	maxBackoff          = 5 * time.Minute
)

// Config configures a Poller; zero-valued fields take the defaults above.
type Config struct {
	BaseURL string
	AgentID string

	// AuthFn returns the per-request bearer token (the agent cert fingerprint),
	// exactly as the replicator/reconcile clients use. Called per-request.
	AuthFn func() string

	HTTP     *http.Client
	Executor *Executor

	// RateProvider returns the per-agent staging-upload rate cap (bytes/sec, 0 =
	// unlimited) from the cached central policy, read at the START of each
	// stage_upload (P10-T4). Nil => unlimited. A mid-upload policy change applies
	// on the next upload (documented).
	RateProvider func() int64

	// MaxCommands drained per poll (default 10); Interval between polls (default
	// 60s); LeaseSeconds is the picked_up lease whose third is the ack-heartbeat
	// cadence during a slow content hash (default 300 -> heartbeat every ~100s).
	MaxCommands  int
	Interval     time.Duration
	LeaseSeconds int

	Logger *slog.Logger
	// Clock/Rand are injectable for deterministic tests (nil => time.Now / a
	// package rand source).
	Clock func() time.Time
	Rand  *rand.Rand
}

// Poller drains central's per-agent command queue and executes each command.
type Poller struct {
	baseURL      string
	agentID      string
	authFn       func() string
	http         *http.Client
	exec         *Executor
	rateProvider func() int64
	maxCmds      int
	interval     time.Duration
	leaseSecs    int
	log          *slog.Logger
	clock        func() time.Time
	rnd          *rand.Rand
}

// NewPoller wires a Poller, applying defaults.
func NewPoller(cfg Config) *Poller {
	p := &Poller{
		baseURL:      strings.TrimRight(cfg.BaseURL, "/"),
		agentID:      cfg.AgentID,
		authFn:       cfg.AuthFn,
		http:         cfg.HTTP,
		exec:         cfg.Executor,
		rateProvider: cfg.RateProvider,
		maxCmds:      cfg.MaxCommands,
		interval:     cfg.Interval,
		leaseSecs:    cfg.LeaseSeconds,
		log:          cfg.Logger,
		clock:        cfg.Clock,
		rnd:          cfg.Rand,
	}
	if p.http == nil {
		p.http = &http.Client{Timeout: defaultTimeout}
	}
	if p.maxCmds <= 0 {
		p.maxCmds = defaultMaxCommands
	}
	if p.interval <= 0 {
		p.interval = defaultInterval
	}
	if p.leaseSecs <= 0 {
		p.leaseSecs = defaultLeaseSeconds
	}
	if p.log == nil {
		p.log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if p.clock == nil {
		p.clock = time.Now
	}
	if p.rnd == nil {
		p.rnd = rand.New(rand.NewSource(time.Now().UnixNano()))
	}
	if p.authFn == nil {
		p.authFn = func() string { return "" }
	}
	return p
}

// commandOut is the subset of central's CommandOut the agent consumes. The rest
// (status/attempts/timestamps) is ignored here.
type commandOut struct {
	ID      string         `json:"id"`
	Kind    string         `json:"kind"`
	ItemID  string         `json:"item_id"`
	Payload map[string]any `json:"payload"`
}

// Run polls until ctx is cancelled: a plain poll every Interval (±10% jitter),
// backing off (capped) while central is unreachable and resetting on the first
// success. A shutdown between polls is clean; a command mid-execution finishes
// (its complete uses a detached ctx) or is redelivered by central's sweep.
func (p *Poller) Run(ctx context.Context) error {
	backoff := time.Duration(0)
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		_, err := p.PollOnce(ctx)
		var wait time.Duration
		if err != nil {
			if backoff == 0 {
				backoff = p.interval
			} else {
				backoff *= 2
			}
			if backoff > maxBackoff {
				backoff = maxBackoff
			}
			p.log.Warn("command poll failed; backing off", "backoff", backoff.String(), "err", err)
			wait = backoff
		} else {
			backoff = 0
			wait = p.jittered(p.interval)
		}
		if !sleepCtx(ctx, wait) {
			return ctx.Err()
		}
	}
}

// PollOnce drains one poll of commands and processes each. It returns the number
// of commands processed and an error ONLY when the poll request itself failed
// (central-down / non-200) — a per-command execute/complete failure is logged and
// never aborts the batch (the sweep redelivers an un-completed command).
func (p *Poller) PollOnce(ctx context.Context) (int, error) {
	cmds, err := p.poll(ctx)
	if err != nil {
		return 0, err
	}
	for _, cmd := range cmds {
		p.process(ctx, cmd)
	}
	return len(cmds), nil
}

// process executes one command and reports its terminal result. Unknown/
// unsupported kinds complete ok=false with a note (never left dangling).
func (p *Poller) process(ctx context.Context, cmd commandOut) {
	switch cmd.Kind {
	case KindStatCheck, KindRehashCheck:
		p.processVerify(ctx, cmd)
	case KindStageUpload:
		p.processStageUpload(ctx, cmd)
	default:
		p.complete(ctx, cmd.ID, false, map[string]any{"error": fmt.Sprintf("unknown command kind %q", cmd.Kind)})
	}
}

// processVerify runs a stat_check / rehash_check, heartbeating the lease during a
// (potentially slow) rehash so central's redelivery sweep does not reclaim it.
func (p *Poller) processVerify(ctx context.Context, cmd commandOut) {
	var (
		res   CommandResult
		exErr error
	)
	if cmd.Kind == KindRehashCheck {
		// A big content hash can outlast the lease: heartbeat every lease/3 while
		// it runs. stat_check is a single stat and never needs it.
		hbCtx, cancel := context.WithCancel(ctx)
		go p.heartbeat(hbCtx, cmd.ID)
		res, exErr = p.exec.Execute(ctx, cmd.Kind, cmd.Payload)
		cancel()
	} else {
		res, exErr = p.exec.Execute(ctx, cmd.Kind, cmd.Payload)
	}

	if exErr != nil {
		// "Cannot answer" (unknown root / traversal / IO error): fail the command
		// so central does not reconcile a wrong answer.
		p.log.Warn("verify command refused", "command_id", cmd.ID, "kind", cmd.Kind, "err", exErr)
		p.complete(ctx, cmd.ID, false, map[string]any{"error": exErr.Error()})
		return
	}
	p.complete(ctx, cmd.ID, true, resultMap(res))
}

// heartbeat acks the command every lease/3 until ctx is cancelled (the execute
// returns). ack failures are logged, never fatal.
func (p *Poller) heartbeat(ctx context.Context, commandID string) {
	interval := time.Duration(p.leaseSecs) * time.Second / 3
	if interval <= 0 {
		interval = time.Second
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if err := p.ack(context.WithoutCancel(ctx), commandID); err != nil {
				p.log.Warn("command lease heartbeat (ack) failed", "command_id", commandID, "err", err)
			}
		}
	}
}

// --- transport -------------------------------------------------------------

func (p *Poller) poll(ctx context.Context) ([]commandOut, error) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/commands/poll", p.baseURL, p.agentID)
	status, body, err := p.post(ctx, url, map[string]any{"max": p.maxCmds})
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, p.statusError("poll", status, body)
	}
	var out []commandOut
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("poll: decode body: %w", err)
	}
	return out, nil
}

func (p *Poller) ack(ctx context.Context, commandID string) error {
	url := fmt.Sprintf("%s/api/v1/agents/%s/commands/%s/ack", p.baseURL, p.agentID, commandID)
	status, body, err := p.post(ctx, url, nil)
	if err != nil {
		return err
	}
	if status != http.StatusOK {
		return p.statusError("ack", status, body)
	}
	return nil
}

// complete reports the terminal result. It uses a detached ctx so a shutdown
// racing the report still records it (mirrors the replicator's MarkSent posture).
func (p *Poller) complete(ctx context.Context, commandID string, ok bool, result map[string]any) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/commands/%s/complete", p.baseURL, p.agentID, commandID)
	body := map[string]any{"ok": ok, "result": result}
	status, resp, err := p.post(context.WithoutCancel(ctx), url, body)
	if err != nil {
		p.log.Warn("command complete failed", "command_id", commandID, "err", err)
		return
	}
	if status != http.StatusOK {
		p.log.Warn("command complete rejected", "command_id", commandID, "err", p.statusError("complete", status, resp))
	}
}

func (p *Poller) post(ctx context.Context, url string, body any) (int, []byte, error) {
	var reader io.Reader
	if body != nil {
		buf, err := json.Marshal(body)
		if err != nil {
			return 0, nil, fmt.Errorf("marshal body: %w", err)
		}
		reader = bytes.NewReader(buf)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, reader)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if tok := p.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	resp, err := p.http.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return resp.StatusCode, respBody, nil
}

func (p *Poller) statusError(step string, status int, body []byte) error {
	detail := centralDetail(body)
	switch status {
	case http.StatusNotFound:
		return fmt.Errorf("commands %s: 404 — agent-command feature disabled or command gone: %s", step, detail)
	case http.StatusUnauthorized, http.StatusForbidden:
		return fmt.Errorf("commands %s: central rejected the agent bearer token (%d): %s", step, status, detail)
	default:
		return fmt.Errorf("commands %s: central returned %d: %s", step, status, detail)
	}
}

// jittered returns d ±10% so a fleet of agents does not poll in lockstep.
func (p *Poller) jittered(d time.Duration) time.Duration {
	if d <= 0 {
		return d
	}
	delta := float64(d) * 0.1
	return d + time.Duration((p.rnd.Float64()*2-1)*delta)
}

// resultMap converts a CommandResult to the map central's complete endpoint
// stores as the row's result JSONB (== the CommandResult contract).
func resultMap(r CommandResult) map[string]any {
	m := map[string]any{"exists": r.Exists, "content_skipped": r.ContentSkipped}
	if r.Size != nil {
		m["size"] = *r.Size
	}
	if r.Mtime != nil {
		m["mtime"] = *r.Mtime
	}
	if r.QuickHash != nil {
		m["quick_hash"] = *r.QuickHash
	}
	if r.ContentHash != nil {
		m["content_hash"] = *r.ContentHash
	}
	return m
}

// centralDetail unwraps a FastAPI {"detail": ...} envelope for logging.
func centralDetail(body []byte) string {
	var env struct {
		Detail json.RawMessage `json:"detail"`
	}
	if json.Unmarshal(body, &env) == nil && len(env.Detail) > 0 {
		var s string
		if json.Unmarshal(env.Detail, &s) == nil && s != "" {
			return s
		}
		return string(env.Detail)
	}
	if len(body) > 512 {
		body = body[:512]
	}
	return string(body)
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
