// Package clicore is the client half of the P7-T3 `filearr query` CLI. It dials
// the P7-T2 same-user local transport (a Unix domain socket on linux/darwin, a
// current-user named pipe on Windows), issues the read-only POST /v1/query
// request, and renders the QueryResponse for a terminal.
//
// This is the SUPPORTED local query surface. The legacy `search` subcommand opens
// the SQLite index file directly (the P7-T1 path); that bypasses the P7-T2 policy
// gate (local_access_enabled) and the peer-credential boundary, so it is retained
// only for local debugging. New callers go through the transport served here.
//
// Offline guarantee: every request rides a filesystem/namespace object the OS
// access-controls — never a network socket — so `filearr query` answers "where
// did I put that file" with central fully unreachable (brief §3.1).
package clicore

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/filearr/filearr/agent/internal/localapi"
)

const (
	// requestTimeout bounds a single request against the local transport. The
	// query runs on a same-machine read-only index (quick); this mostly guards
	// against a hung/half-open pipe or socket.
	requestTimeout = 15 * time.Second
	// baseURL is a placeholder authority. The request rides the pipe/socket dialer
	// (platformDial), so the host is never resolved — there is no network hop and
	// no DNS to rebind (brief §3.1).
	baseURL = "http://filearr-agent-localapi"
	// maxErrBody caps how much of an error envelope we read (defensive).
	maxErrBody = 1 << 20
)

// TransportError wraps a dial/connection failure so the CLI can render the
// actionable "is the agent running?" message distinctly from a server-side query
// error.
type TransportError struct{ Err error }

func (e *TransportError) Error() string { return e.Err.Error() }
func (e *TransportError) Unwrap() error { return e.Err }

// QueryError is a structured error the agent returned (parse/exec/policy). It
// mirrors the localapi error envelope, which is unexported in that package.
type QueryError struct {
	Status   int
	Code     string
	Message  string
	Position *int
	Reason   string
	Keys     []string
}

func (e *QueryError) Error() string {
	if e.Message != "" {
		return e.Message
	}
	if e.Code != "" {
		return e.Code
	}
	return fmt.Sprintf("agent returned HTTP %d", e.Status)
}

// Client talks to one machine's agent over the local transport. Construct with
// Dial; it holds no live connection until the first request.
type Client struct {
	hc   *http.Client
	base string
}

// Dial builds a Client bound to the transport at path — a Unix socket path
// (linux/darwin) or a Windows named-pipe name. Resolve the default path with
// localapi.DefaultPath when the user passes no --socket override.
func Dial(path string) *Client {
	return newClient(&http.Client{
		Timeout:   requestTimeout,
		Transport: &http.Transport{DialContext: platformDial(path)},
	})
}

// newClient is the test seam: it binds a Client to an arbitrary http.Client (one
// whose transport dials a test pipe/socket).
func newClient(hc *http.Client) *Client { return &Client{hc: hc, base: baseURL} }

// Query issues POST /v1/query and decodes the response. A dial/connection failure
// becomes *TransportError; a non-200 JSON envelope becomes *QueryError.
func (c *Client) Query(ctx context.Context, req localapi.QueryRequest) (localapi.QueryResponse, error) {
	var out localapi.QueryResponse
	body, err := json.Marshal(req)
	if err != nil {
		return out, err
	}
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.base+"/v1/query", bytes.NewReader(body))
	if err != nil {
		return out, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := c.hc.Do(httpReq)
	if err != nil {
		return out, &TransportError{Err: err}
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return out, decodeErr(resp)
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return out, fmt.Errorf("decode query response: %w", err)
	}
	return out, nil
}

// Health issues GET /v1/health — the CLI's connectivity/index-readiness probe.
func (c *Client) Health(ctx context.Context) (localapi.HealthResponse, error) {
	var out localapi.HealthResponse
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/v1/health", nil)
	if err != nil {
		return out, err
	}
	resp, err := c.hc.Do(httpReq)
	if err != nil {
		return out, &TransportError{Err: err}
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return out, decodeErr(resp)
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return out, fmt.Errorf("decode health response: %w", err)
	}
	return out, nil
}

// History issues GET /v1/history?limit=N — the local-only frecency suggestions
// (top past queries on THIS machine). Search history never leaves the machine; it
// is served over the same same-user socket/pipe as every other route.
func (c *Client) History(ctx context.Context, limit int) (localapi.HistoryResponse, error) {
	var out localapi.HistoryResponse
	url := fmt.Sprintf("%s/v1/history?limit=%d", c.base, limit)
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return out, err
	}
	resp, err := c.hc.Do(httpReq)
	if err != nil {
		return out, &TransportError{Err: err}
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return out, decodeErr(resp)
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return out, fmt.Errorf("decode history response: %w", err)
	}
	return out, nil
}

// wireError mirrors localapi's unexported errorBody envelope (snake_case keys).
type wireError struct {
	Error    string   `json:"error"`
	Code     string   `json:"code"`
	Position *int     `json:"position"`
	Reason   string   `json:"reason"`
	Keys     []string `json:"keys"`
}

func decodeErr(resp *http.Response) error {
	var we wireError
	buf, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrBody))
	_ = json.Unmarshal(buf, &we)
	return &QueryError{
		Status:   resp.StatusCode,
		Code:     we.Code,
		Message:  we.Error,
		Position: we.Position,
		Reason:   we.Reason,
		Keys:     we.Keys,
	}
}
