package taxonomy

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

const defaultFetchTimeout = 30 * time.Second

// ClientConfig configures a Client. It mirrors config.ClientConfig (the policy
// poll client) so the daemon can wire both from the same central URL / agent id
// / interim bearer provider / shared mTLS-aware HTTP client.
type ClientConfig struct {
	BaseURL string // central root; /api/v1 is appended internally
	AgentID string
	AuthFn  func() string // per-request bearer (interim cert-fingerprint scheme)
	HTTP    *http.Client
	Logger  *slog.Logger
}

// Client fetches the compact agent taxonomy from
// GET /api/v1/agents/{id}/taxonomy. It is the fetch seam the Cache calls when a
// policy's taxonomy_version exceeds the cached snapshot.
type Client struct {
	baseURL string
	agentID string
	authFn  func() string
	http    *http.Client
	log     *slog.Logger
}

// NewClient wires a Client; zero-valued fields take sane defaults.
func NewClient(cfg ClientConfig) *Client {
	c := &Client{
		baseURL: strings.TrimRight(cfg.BaseURL, "/"),
		agentID: cfg.AgentID,
		authFn:  cfg.AuthFn,
		http:    cfg.HTTP,
		log:     cfg.Logger,
	}
	if c.http == nil {
		c.http = &http.Client{Timeout: defaultFetchTimeout}
	}
	if c.authFn == nil {
		c.authFn = func() string { return "" }
	}
	if c.log == nil {
		c.log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	return c
}

// Fetch does one GET of the compact taxonomy payload and parses it into a
// snapshot. Returns an error on any non-200 status or transport/parse failure;
// the caller keeps its current snapshot on error.
func (c *Client) Fetch(ctx context.Context) (*Taxonomy, error) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/taxonomy", c.baseURL, c.agentID)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	if tok := c.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	// The payload is ~1271 ext entries; cap the read generously (a few hundred KiB).
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("central returned %d for taxonomy: %s", resp.StatusCode, detail(body))
	}
	return ParsePayload(body)
}

// detail unwraps a FastAPI {"detail": "..."} envelope for logging, trimming an
// oversized body.
func detail(body []byte) string {
	if len(body) > 512 {
		body = body[:512]
	}
	return string(body)
}
