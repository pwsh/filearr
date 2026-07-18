package main

import (
	"fmt"
	"os"
	"path/filepath"
	"time"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
)

// config holds the resolved agent settings. Every field is set from a command
// flag with an FILEARR_AGENT_* environment fallback (flag wins when both are
// present). stdlib flag only — no CLI framework (that decision is deferred to
// P7-T3).
type config struct {
	CentralURL string // central Filearr base URL, e.g. https://filearr.example.com
	Token      string // single-use enrollment token (enroll only)
	DataDir    string // where key/cert/state are persisted
	Name       string // optional friendly agent name
}

const (
	envCentralURL        = "FILEARR_AGENT_CENTRAL_URL"
	envToken             = "FILEARR_AGENT_TOKEN"
	envDataDir           = "FILEARR_AGENT_DATA_DIR"
	envName              = "FILEARR_AGENT_NAME"
	envReconcileInterval = "FILEARR_AGENT_RECONCILE_INTERVAL"
)

// defaultReconcileInterval is the slow periodic full-manifest sweep cadence and
// the reconnect-outage threshold that also gates trigger (b). It is the single
// canonical 24h Phase-5 threshold: P7-T4's offline-grace default REUSES it
// (config.DefaultOfflineGrace is defined as this same value, R4 — not a second
// constant).
const defaultReconcileInterval = agentcfg.DefaultOfflineGrace

// reconcileInterval resolves FILEARR_AGENT_RECONCILE_INTERVAL (a Go duration such
// as "24h" or "30m"), falling back to defaultReconcileInterval when unset or
// unparseable/non-positive.
func reconcileInterval() time.Duration {
	if v := os.Getenv(envReconcileInterval); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			return d
		}
	}
	return defaultReconcileInterval
}

// defaultDataDir returns an OS-appropriate per-user config location, e.g.
// %AppData%\filearr-agent on Windows, ~/.config/filearr-agent on Linux,
// ~/Library/Application Support/filearr-agent on macOS.
func defaultDataDir() string {
	base, err := os.UserConfigDir()
	if err != nil || base == "" {
		return "filearr-agent" // last-resort relative dir; flag/env can override
	}
	return filepath.Join(base, "filearr-agent")
}

// envOr returns the environment value for key, or fallback when unset/empty.
func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// requireCentralURL validates the enroll/run precondition.
func (c *config) requireCentralURL() error {
	if c.CentralURL == "" {
		return fmt.Errorf("central URL is required (-central or %s)", envCentralURL)
	}
	return nil
}
