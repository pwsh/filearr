package reconcile

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sort"
	"strings"
	"time"
)

// Protocol/page bounds (FROZEN client contract). The rows endpoint caps a page at
// maxPageSize server-side; the client defaults to defaultPageSize and halves on a
// 413 down to minPageSize.
const (
	defaultPageSize = 2000
	maxPageSize     = 5000
	minPageSize     = 1
	defaultTimeout  = 60 * time.Second
)

// Sentinel errors the Sweeper routes on.
var (
	// ErrSessionExpired is a 404 from a rows/finish call — the central session
	// lapsed (TTL) and the sweep must restart from `start`.
	ErrSessionExpired = errors.New("reconcile session expired (404)")
	// ErrDigestMismatch is a 409 from finish — the manifest central assembled from
	// the streamed rows does not hash to the digest the agent committed to (the
	// corpus changed mid-sweep). Restart the sweep once, then surface.
	ErrDigestMismatch = errors.New("reconcile finish digest mismatch (409)")
)

// ClientConfig configures a reconcile protocol Client; zero fields take defaults.
type ClientConfig struct {
	BaseURL  string
	AgentID  string
	AuthFn   func() string // per-request bearer token (agent cert fingerprint)
	HTTP     *http.Client
	PageSize int // rows per page; clamped to [minPageSize, maxPageSize]
	Logger   *slog.Logger
}

// Client speaks the three-step reconcile protocol (start/rows/finish) against
// central, mirroring the replicator's bearer-auth transport.
type Client struct {
	baseURL  string
	agentID  string
	authFn   func() string
	http     *http.Client
	pageSize int
	log      *slog.Logger
}

// NewClient wires a Client, applying defaults.
func NewClient(cfg ClientConfig) *Client {
	c := &Client{
		baseURL:  strings.TrimRight(cfg.BaseURL, "/"),
		agentID:  cfg.AgentID,
		authFn:   cfg.AuthFn,
		http:     cfg.HTTP,
		pageSize: cfg.PageSize,
		log:      cfg.Logger,
	}
	if c.http == nil {
		c.http = &http.Client{Timeout: defaultTimeout}
	}
	if c.pageSize <= 0 {
		c.pageSize = defaultPageSize
	}
	if c.pageSize > maxPageSize {
		c.pageSize = maxPageSize
	}
	if c.log == nil {
		c.log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if c.authFn == nil {
		c.authFn = func() string { return "" }
	}
	return c
}

// --- wire bodies -----------------------------------------------------------

type startRequest struct {
	LibraryRef string `json:"library_ref"`
	Digest     string `json:"digest"`
	RowCount   int    `json:"row_count"`
	Rebuilt    bool   `json:"rebuilt"`
}

type startResponse struct {
	Status    string `json:"status"` // "match" | "mismatch"
	SessionID string `json:"session_id"`
}

// wireRow is one row on the rows endpoint: mtime is FLOAT epoch seconds (the same
// value replication sends), NOT the digest's integer microseconds. Absent hashes
// marshal to JSON null.
type wireRow struct {
	RelPath     string  `json:"rel_path"`
	Size        int64   `json:"size"`
	Mtime       float64 `json:"mtime"`
	QuickHash   *string `json:"quick_hash"`
	ContentHash *string `json:"content_hash"`
}

type rowsRequest struct {
	Rows []wireRow `json:"rows"`
}

type finishRequest struct {
	Digest   string `json:"digest"`
	RowCount int    `json:"row_count"`
	ResetSeq bool   `json:"reset_seq"`
}

// FinishResult is central's finish body: a status plus opaque counters the sweep
// passes through to the caller/CLI.
type FinishResult struct {
	Status   string
	Counters map[string]any
}

// SortedCounters renders the counters as a stable "k=v k=v" string for logging.
func (f FinishResult) SortedCounters() string {
	keys := make([]string, 0, len(f.Counters))
	for k := range f.Counters {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for i, k := range keys {
		if i > 0 {
			b.WriteByte(' ')
		}
		fmt.Fprintf(&b, "%s=%v", k, f.Counters[k])
	}
	return b.String()
}

// --- protocol steps --------------------------------------------------------

// Start posts the digest. Returns matched=true (nothing more to do) or a
// sessionID to stream rows into.
func (c *Client) Start(ctx context.Context, libraryRef, digest string, rowCount int, rebuilt bool) (matched bool, sessionID string, err error) {
	body := startRequest{LibraryRef: libraryRef, Digest: digest, RowCount: rowCount, Rebuilt: rebuilt}
	status, resp, err := c.post(ctx, fmt.Sprintf("/api/v1/agents/%s/reconcile/start", c.agentID), body)
	if err != nil {
		return false, "", err
	}
	if status != http.StatusOK {
		return false, "", c.statusError("start", status, resp)
	}
	var sr startResponse
	if err := json.Unmarshal(resp, &sr); err != nil {
		return false, "", fmt.Errorf("reconcile start: decode body: %w", err)
	}
	switch sr.Status {
	case "match":
		return true, "", nil
	case "mismatch":
		if sr.SessionID == "" {
			return false, "", fmt.Errorf("reconcile start: mismatch without session_id")
		}
		return false, sr.SessionID, nil
	default:
		return false, "", fmt.Errorf("reconcile start: unexpected status %q", sr.Status)
	}
}

// SendRows streams every row to central in pages, halving the page size on a 413
// and retrying, and reporting a 404 as ErrSessionExpired. Page size shrinks stay
// in effect for the remainder of the stream.
func (c *Client) SendRows(ctx context.Context, sessionID string, rows []Row) error {
	url := fmt.Sprintf("/api/v1/agents/%s/reconcile/%s/rows", c.agentID, sessionID)
	page := c.pageSize
	for i := 0; i < len(rows); {
		end := i + page
		if end > len(rows) {
			end = len(rows)
		}
		status, resp, err := c.post(ctx, url, rowsRequest{Rows: toWireRows(rows[i:end])})
		if err != nil {
			return err
		}
		switch status {
		case http.StatusOK, http.StatusAccepted, http.StatusNoContent:
			i = end
		case http.StatusNotFound:
			return ErrSessionExpired
		case http.StatusRequestEntityTooLarge:
			if page <= minPageSize {
				return c.statusError("rows", status, resp)
			}
			page /= 2
			if page < minPageSize {
				page = minPageSize
			}
			c.log.Warn("reconcile rows page too large (413); halving", "new_page", page)
			// retry the same window with the smaller page (i unchanged)
		default:
			return c.statusError("rows", status, resp)
		}
	}
	return nil
}

// Finish commits the sweep. reset_seq=true tells central to reset the agent's
// replication watermark (the outbox backlog is superseded). Returns the counters,
// ErrSessionExpired on 404, or ErrDigestMismatch on a 409 digest mismatch.
func (c *Client) Finish(ctx context.Context, sessionID, digest string, rowCount int, resetSeq bool) (FinishResult, error) {
	url := fmt.Sprintf("/api/v1/agents/%s/reconcile/%s/finish", c.agentID, sessionID)
	body := finishRequest{Digest: digest, RowCount: rowCount, ResetSeq: resetSeq}
	status, resp, err := c.post(ctx, url, body)
	if err != nil {
		return FinishResult{}, err
	}
	switch status {
	case http.StatusOK:
		var raw map[string]json.RawMessage
		if err := json.Unmarshal(resp, &raw); err != nil {
			return FinishResult{}, fmt.Errorf("reconcile finish: decode body: %w", err)
		}
		res := FinishResult{Counters: map[string]any{}}
		for k, v := range raw {
			if k == "status" {
				_ = json.Unmarshal(v, &res.Status)
				continue
			}
			var val any
			_ = json.Unmarshal(v, &val)
			res.Counters[k] = val
		}
		return res, nil
	case http.StatusNotFound:
		return FinishResult{}, ErrSessionExpired
	case http.StatusConflict:
		// Distinguish a digest-mismatch 409 from other conflicts by reason.
		if reason := centralReason(resp); reason == "" || strings.Contains(reason, "digest") {
			return FinishResult{}, ErrDigestMismatch
		}
		return FinishResult{}, c.statusError("finish", status, resp)
	default:
		return FinishResult{}, c.statusError("finish", status, resp)
	}
}

// --- transport -------------------------------------------------------------

func (c *Client) post(ctx context.Context, path string, body any) (int, []byte, error) {
	buf, err := json.Marshal(body)
	if err != nil {
		return 0, nil, fmt.Errorf("marshal %s: %w", path, err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(buf))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if tok := c.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return resp.StatusCode, respBody, nil
}

func (c *Client) statusError(step string, status int, body []byte) error {
	detail := centralDetail(body)
	switch status {
	case http.StatusUnauthorized, http.StatusForbidden:
		return fmt.Errorf("reconcile %s: central rejected the agent bearer token (%d): %s", step, status, detail)
	case http.StatusNotFound:
		return fmt.Errorf("reconcile %s: 404 — endpoint disabled or session gone: %s", step, detail)
	default:
		return fmt.Errorf("reconcile %s: central returned %d: %s", step, status, detail)
	}
}

func toWireRows(rows []Row) []wireRow {
	out := make([]wireRow, 0, len(rows))
	for _, r := range rows {
		wr := wireRow{RelPath: r.RelPath, Size: r.Size, Mtime: r.mtimeSeconds()}
		if r.QuickHash != "" {
			q := r.QuickHash
			wr.QuickHash = &q
		}
		if r.ContentHash != "" {
			ch := r.ContentHash
			wr.ContentHash = &ch
		}
		out = append(out, wr)
	}
	return out
}

// centralDetail unwraps a FastAPI {"detail": ...} envelope for logging. detail may
// be a string or an object (validation errors); both are rendered.
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

// centralReason pulls a machine reason string from a {"detail":{"reason":...}} or
// {"reason":...} body, used to classify a 409.
func centralReason(body []byte) string {
	var flat struct {
		Reason string `json:"reason"`
	}
	if json.Unmarshal(body, &flat) == nil && flat.Reason != "" {
		return flat.Reason
	}
	var nested struct {
		Detail struct {
			Reason string `json:"reason"`
		} `json:"detail"`
	}
	if json.Unmarshal(body, &nested) == nil && nested.Detail.Reason != "" {
		return nested.Detail.Reason
	}
	return ""
}
