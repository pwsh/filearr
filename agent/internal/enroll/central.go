package enroll

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// CentralClient speaks the agent plane of the central Filearr API
// (backend/filearr/api/agents.py). These two endpoints are NOT API-key gated:
// the enrollment token authenticates /register and the one-time enroll_secret
// authenticates /certificate. All bodies are snake_case JSON.
type CentralClient struct {
	// BaseURL is the central server root, e.g. https://filearr.example.com
	// (no trailing /api). A trailing slash is tolerated.
	BaseURL string
	HTTP    *http.Client
}

// NewCentralClient returns a client with a sane default timeout.
func NewCentralClient(baseURL string) *CentralClient {
	return &CentralClient{
		BaseURL: strings.TrimRight(baseURL, "/"),
		HTTP:    &http.Client{Timeout: 30 * time.Second},
	}
}

func (c *CentralClient) httpClient() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return http.DefaultClient
}

// RegisterRequest mirrors backend RegisterIn.
type RegisterRequest struct {
	Token        string `json:"token"`
	Hostname     string `json:"hostname"`
	Platform     string `json:"platform"`
	Name         string `json:"name,omitempty"`
	AgentVersion string `json:"agent_version,omitempty"`
}

// CABootstrap mirrors backend CaBootstrap: public pinning material, never a
// secret.
type CABootstrap struct {
	URL          string `json:"url"`
	Fingerprint  string `json:"fingerprint"`
	Provisioner  string `json:"provisioner"`
	CertTTLHours int    `json:"cert_ttl_hours"`
}

// RegisterResponse mirrors backend RegisterOut. CaOTT is nil when central's
// provisioner JWK is unset/malformed (the documented fail-safe: registration
// still succeeds but no cert can be minted until an operator re-issues one).
type RegisterResponse struct {
	AgentID      string      `json:"agent_id"`
	RolloutGroup string      `json:"rollout_group"`
	Status       string      `json:"status"`
	EnrollSecret string      `json:"enroll_secret"`
	CA           CABootstrap `json:"ca"`
	CaOTT        *string     `json:"ca_ott"`
}

// BindRequest mirrors backend CertBindIn.
type BindRequest struct {
	EnrollSecret    string `json:"enroll_secret"`
	CertFingerprint string `json:"cert_fingerprint"`
}

// AgentResponse mirrors the fields of backend AgentOut we care about.
type AgentResponse struct {
	ID              string `json:"id"`
	Status          string `json:"status"`
	CertFingerprint string `json:"cert_fingerprint"`
}

// Register performs the register-first handshake step (docs/ops/agents.md §3.1):
// the token IS the credential, so no bearer auth is sent. On success central has
// consumed the token, assigned the authoritative agent_id, and returned the CA
// bootstrap material plus a one-time enroll_secret.
func (c *CentralClient) Register(ctx context.Context, req RegisterRequest) (*RegisterResponse, error) {
	var out RegisterResponse
	if err := c.postJSON(ctx, "/api/v1/agents/register", req, &out); err != nil {
		return nil, fmt.Errorf("register: %w", err)
	}
	return &out, nil
}

// BindCertificate binds the CA-issued fingerprint to the pending agent
// (pending -> active). The one-time enroll_secret guards against a guessed
// agent UUID hijacking the pending identity.
func (c *CentralClient) BindCertificate(ctx context.Context, agentID string, req BindRequest) (*AgentResponse, error) {
	var out AgentResponse
	path := "/api/v1/agents/" + agentID + "/certificate"
	if err := c.postJSON(ctx, path, req, &out); err != nil {
		return nil, fmt.Errorf("bind certificate: %w", err)
	}
	return &out, nil
}

func (c *CentralClient) postJSON(ctx context.Context, path string, body, out any) error {
	buf, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("marshal request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+path, bytes.NewReader(buf))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := c.httpClient().Do(req)
	if err != nil {
		return fmt.Errorf("POST %s: %w", path, err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode >= 300 {
		return &HTTPError{Status: resp.StatusCode, Path: path, Body: string(respBody)}
	}
	if out != nil {
		if err := json.Unmarshal(respBody, out); err != nil {
			return fmt.Errorf("decode response from %s: %w", path, err)
		}
	}
	return nil
}

// HTTPError carries a non-2xx central response. The central error detail is a
// short machine-mappable string (e.g. "enrollment token consumed") so it is
// safe and useful to surface.
type HTTPError struct {
	Status int
	Path   string
	Body   string
}

func (e *HTTPError) Error() string {
	detail := e.Body
	// Central errors are FastAPI {"detail": "..."} envelopes; unwrap for a
	// cleaner message when present.
	var env struct {
		Detail string `json:"detail"`
	}
	if json.Unmarshal([]byte(e.Body), &env) == nil && env.Detail != "" {
		detail = env.Detail
	}
	return fmt.Sprintf("central returned %d for %s: %s", e.Status, e.Path, detail)
}
