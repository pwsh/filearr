package update

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

// client talks to central's agent-plane update endpoints, reusing the shared
// mTLS/bearer HTTP client + per-request auth token (the same seam replication /
// policy / commands use).
type client struct {
	baseURL string
	agentID string
	authFn  func() string
	http    *http.Client
}

// fetchManifest GETs the newest release manifest that covers this agent and is
// newer than “current“. Central answers 200 with the signed manifest, or 204
// when there is nothing newer. “current“ is reported to central as the running
// version — that report IS the §6.3 confirmed-version signal (central records it
// on “agents.agent_version“), so a normal poll doubles as a health confirm.
func (c *client) fetchManifest(ctx context.Context, current string) (*Manifest, error) {
	u := fmt.Sprintf("%s/api/v1/agents/%s/update-manifest?current=%s",
		c.baseURL, c.agentID, url.QueryEscape(current))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
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
	switch resp.StatusCode {
	case http.StatusNoContent:
		return nil, nil // up to date
	case http.StatusOK:
		body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		if err != nil {
			return nil, fmt.Errorf("read manifest: %w", err)
		}
		m, err := ParseManifest(body)
		if err != nil {
			return nil, err
		}
		return &m, nil
	default:
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("manifest fetch: central returned %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
}

// downloadArtifact streams the artifact to a temp file under dir, verifying its
// sha256 against the (signature-verified) manifest artifact. A mismatch removes
// the temp file and errors — the swap never runs (accept criterion: a sha256
// mismatch refuses to swap). “a.URL“ is treated as an opaque filename; it is
// path-escaped so a hostile manifest filename can never traverse central's
// artifact directory (central also enforces this server-side).
func (c *client) downloadArtifact(ctx context.Context, version string, a Artifact, dir string) (string, error) {
	u := fmt.Sprintf("%s/api/v1/agents/%s/releases/%s/artifacts/%s",
		c.baseURL, c.agentID, url.PathEscape(version), url.PathEscape(a.URL))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return "", err
	}
	if tok := c.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return "", fmt.Errorf("artifact download: central returned %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", fmt.Errorf("create download dir: %w", err)
	}
	tmp, err := os.CreateTemp(dir, "download-*")
	if err != nil {
		return "", err
	}
	tmpName := tmp.Name()
	h := sha256.New()
	// Bound the read by the manifest-declared size (+1 to detect an overrun) so a
	// hostile central cannot stream an unbounded body onto the agent's disk.
	limit := a.Size + 1
	if a.Size <= 0 {
		limit = 1 << 40 // no declared size: a generous 1 TiB backstop
	}
	n, copyErr := io.Copy(io.MultiWriter(tmp, h), io.LimitReader(resp.Body, limit))
	syncErr := tmp.Sync()
	closeErr := tmp.Close()
	if copyErr != nil {
		_ = os.Remove(tmpName)
		return "", fmt.Errorf("download artifact: %w", copyErr)
	}
	if syncErr != nil {
		_ = os.Remove(tmpName)
		return "", syncErr
	}
	if closeErr != nil {
		_ = os.Remove(tmpName)
		return "", closeErr
	}
	if a.Size > 0 && n != a.Size {
		_ = os.Remove(tmpName)
		return "", fmt.Errorf("download artifact: size mismatch (got %d, want %d)", n, a.Size)
	}
	got := hex.EncodeToString(h.Sum(nil))
	if !strings.EqualFold(got, a.SHA256) {
		_ = os.Remove(tmpName)
		return "", fmt.Errorf("download artifact: sha256 mismatch (got %s, want %s)", got, a.SHA256)
	}
	// Rename to a stable name so a partial ``download-*`` never looks complete.
	final := filepath.Join(dir, "filearr-agent-"+version+artifactSuffix(a))
	if err := os.Rename(tmpName, final); err != nil {
		_ = os.Remove(tmpName)
		return "", err
	}
	return final, nil
}

func artifactSuffix(a Artifact) string {
	if a.Platform == "windows" {
		return ".exe"
	}
	return ""
}
