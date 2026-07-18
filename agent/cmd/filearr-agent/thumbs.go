package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	neturl "net/url"
	"os"
	"os/exec"
	"strconv"
	"time"

	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/scan"
	"github.com/filearr/filearr/agent/internal/thumbs"
)

// Thumbnail-pass env fallbacks (background daemon concern — no operator flags).
const (
	envThumbsEnabled  = "FILEARR_AGENT_THUMBS_ENABLED"  // "false"/"0" disables (default on)
	envThumbsInterval = "FILEARR_AGENT_THUMBS_INTERVAL" // Go duration between passes (default 5m)
	envThumbsRate     = "FILEARR_AGENT_THUMBS_RATE"     // items/sec throttle (default 5)
	envThumbsMaxEdge  = "FILEARR_AGENT_THUMB_MAX_EDGE"  // preview tier longest edge (default central 800)
	envFFmpegPath     = "FILEARR_AGENT_FFMPEG_PATH"     // ffmpeg binary override (default: PATH lookup)
)

// startThumbnailer launches the P12-T13 post-scan thumbnail generation pass for
// the `run` daemon: a low-priority background walk that generates grid+preview
// thumbnails for locally-hosted items and pushes them to central's agent-plane
// small-blob endpoint (write-if-absent, content-addressed). Disabled with
// FILEARR_AGENT_THUMBS_ENABLED=false. Returns a done-channel so the daemon waits
// for a clean stop, mirroring startCommandPoller.
func startThumbnailer(ctx context.Context, idx *index.Store, certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client) <-chan struct{} {
	done := make(chan struct{})
	if !envBool(envThumbsEnabled, true) {
		close(done)
		return done
	}
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 60 * time.Second}
	}

	// Probe ffmpeg ONCE: absent → video thumbnails are skipped (capability-gated),
	// never an error. An explicit override wins over the PATH lookup.
	ffmpegPath := os.Getenv(envFFmpegPath)
	if ffmpegPath == "" {
		if p, err := exec.LookPath("ffmpeg"); err == nil {
			ffmpegPath = p
		}
	}

	tiers := []thumbs.TierSpec{thumbs.GridSpec, previewSpecFromEnv()}
	tn := thumbs.New(thumbs.Config{
		Store: idx,
		Uploader: &thumbUploader{
			baseURL: centralURL,
			agentID: agentID,
			authFn:  authProvider(certStore),
			http:    httpClient,
		},
		FFmpegPath: ffmpegPath,
		Tiers:      tiers,
		IsArtwork:  scan.IsArtworkSidecar,
		Interval:   envDuration(envThumbsInterval, 5*time.Minute),
		RatePerSec: float64(envInt(envThumbsRate, 5)),
		Logger:     newLogger(),
	})
	go func() {
		defer close(done)
		if err := tn.Run(ctx); err != nil && ctx.Err() == nil {
			newLogger().Error("thumbnail pass loop exited", "err", err)
		}
	}()
	return done
}

// previewSpecFromEnv mirrors central's preview tier, allowing the longest edge to
// be overridden (FILEARR_AGENT_THUMB_MAX_EDGE). A drift from central changes only
// the thumbnail's dimensions, never its cache key (the key uses hash:gen:tier).
func previewSpecFromEnv() thumbs.TierSpec {
	spec := thumbs.PreviewSpec
	if e := envInt(envThumbsMaxEdge, 0); e > 0 {
		spec.MaxEdge = e
	}
	return spec
}

// thumbUploader pushes one generated thumbnail to central's agent-plane
// POST /api/v1/agents/{id}/thumbs (write-if-absent, content-addressed). It shares
// the daemon's mTLS/bearer HTTP client.
type thumbUploader struct {
	baseURL string
	agentID string
	authFn  func() string
	http    *http.Client
}

// tierName maps a tier constant to central's ?tier= vocabulary.
func tierName(tier int) string {
	if tier == thumbs.TierPreview {
		return "preview"
	}
	return "grid"
}

// Upload implements thumbs.Uploader. 2xx → stored. 404 (item not yet replicated)
// and 409 (key race: the agent's local hash briefly disagrees with central's
// catalog row) are NON-FATAL declines — stored=false, nil error — so the pass
// retries on a later pass without marking. Any other non-2xx is a transport-level
// error (logged, retried next pass).
//
// The item is referenced by (library_ref, rel_path), percent-encoded into headers
// (a rel_path may hold arbitrary unicode / spaces that a raw header cannot carry).
// Central resolves the item under THIS agent's library, which also authorises
// ownership by construction.
func (u *thumbUploader) Upload(ctx context.Context, libraryRef, relPath string, tier int, key string, tb *thumbs.ThumbBytes) (bool, error) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/thumbs", u.baseURL, u.agentID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(tb.Data))
	if err != nil {
		return false, err
	}
	ct := "image/jpeg"
	if tb.Format == "webp" {
		ct = "image/webp"
	}
	req.Header.Set("Content-Type", ct)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-Filearr-Library-Ref", neturl.QueryEscape(libraryRef))
	req.Header.Set("X-Filearr-Rel-Path", neturl.QueryEscape(relPath))
	req.Header.Set("X-Filearr-Thumb-Tier", tierName(tier))
	req.Header.Set("X-Filearr-Thumb-Key", key)
	req.Header.Set("X-Filearr-Thumb-Width", strconv.Itoa(tb.Width))
	req.Header.Set("X-Filearr-Thumb-Height", strconv.Itoa(tb.Height))
	if tok := u.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}

	resp, err := u.http.Do(req)
	if err != nil {
		return false, err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<16))

	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		return true, nil
	case resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusConflict:
		// Not yet replicated / transient key race: retry a later pass, no mark.
		return false, nil
	default:
		return false, fmt.Errorf("thumb upload: central returned %d", resp.StatusCode)
	}
}

// envBool parses a boolean env var; unset → def. "false"/"0"/"no"/"off" are false.
func envBool(key string, def bool) bool {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	switch v {
	case "0", "false", "False", "FALSE", "no", "off":
		return false
	default:
		return true
	}
}
