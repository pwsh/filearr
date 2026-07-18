package config

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

// ErrNotModified is the sentinel Fetch returns for a 304 (the cached policy is
// still current). The caller keeps its last-known policy and does not re-apply.
var ErrNotModified = errors.New("policy not modified (304)")

const defaultFetchTimeout = 30 * time.Second

// FetchResult is a fresh 200 policy response.
type FetchResult struct {
	ETag    string          // the response ETag header (e.g. `"library/7"`), sent back verbatim as If-None-Match
	Scope   string          // policy scope; "none" means no policy configured
	Version int             // policy version; 0 with scope "none"
	Policy  json.RawMessage // raw policy body — unknown keys preserved
}

// ClientConfig configures a PolicyClient.
type ClientConfig struct {
	// BaseURL is the central root (e.g. https://filearr.example.com); a trailing
	// slash is tolerated and /api/v1 is appended internally.
	BaseURL string
	AgentID string
	// AuthFn returns the bearer token per-request — the same interim cert-
	// fingerprint scheme the replicator uses (cmd authProvider). Called per
	// request so a rotated value is picked up live.
	AuthFn func() string
	HTTP   *http.Client
	Logger *slog.Logger
}

// PolicyClient fetches the agent's policy with ETag conditional-GET semantics.
type PolicyClient struct {
	baseURL string
	agentID string
	authFn  func() string
	http    *http.Client
	log     *slog.Logger
}

// NewPolicyClient wires a PolicyClient; zero-valued fields take sane defaults.
func NewPolicyClient(cfg ClientConfig) *PolicyClient {
	c := &PolicyClient{
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

// Fetch does one conditional GET. etag is the cached ETag (empty on first fetch)
// sent as If-None-Match; appliedVersion is the version the agent has FULLY
// applied, reported via the ?applied= query param so central can stamp
// agents.policy_version_applied. Returns ErrNotModified on 304 (cache current),
// a FetchResult on 200, or an error on any other status / transport failure.
func (c *PolicyClient) Fetch(ctx context.Context, etag string, appliedVersion int) (FetchResult, error) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/policy?applied=%d", c.baseURL, c.agentID, appliedVersion)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return FetchResult{}, err
	}
	req.Header.Set("Accept", "application/json")
	if etag != "" {
		req.Header.Set("If-None-Match", etag)
	}
	if tok := c.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return FetchResult{}, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	switch resp.StatusCode {
	case http.StatusNotModified:
		return FetchResult{}, ErrNotModified
	case http.StatusOK:
		var env struct {
			Scope   string          `json:"scope"`
			Version int             `json:"version"`
			Policy  json.RawMessage `json:"policy"`
		}
		if err := json.Unmarshal(body, &env); err != nil {
			return FetchResult{}, fmt.Errorf("decode policy 200 body: %w", err)
		}
		return FetchResult{
			ETag:    resp.Header.Get("ETag"),
			Scope:   env.Scope,
			Version: env.Version,
			Policy:  normalizeRaw(env.Policy),
		}, nil
	default:
		return FetchResult{}, c.statusError(resp.StatusCode, body)
	}
}

// statusError renders an actionable message per terminal-ish status (all are
// retried with backoff by the Poller; 404 = feature-off is called out).
func (c *PolicyClient) statusError(status int, body []byte) error {
	detail := centralDetail(body)
	switch status {
	case http.StatusNotFound:
		return fmt.Errorf("central policy endpoint returned 404 — the policy feature appears disabled on this server (will retry): %s", detail)
	case http.StatusUnauthorized, http.StatusForbidden:
		return fmt.Errorf("central rejected the agent bearer token (%d) — check the bound cert fingerprint / FILEARR_AGENT_AUTH_FINGERPRINT: %s", status, detail)
	default:
		return fmt.Errorf("central returned %d for policy: %s", status, detail)
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
