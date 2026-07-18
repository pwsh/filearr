package main

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/filearr/filearr/agent/internal/agentlog"
	"github.com/filearr/filearr/agent/internal/commands"
	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/inventory"
	"github.com/filearr/filearr/agent/internal/pathspec"
)

// Command-poller env fallbacks (flags are not plumbed for this loop — it is a
// background daemon concern, not an operator-facing one).
const (
	envCommandPollInterval = "FILEARR_AGENT_COMMAND_POLL_INTERVAL" // Go duration (default 60s)
	envCommandPollMax      = "FILEARR_AGENT_COMMAND_POLL_MAX"      // per-poll drain cap (default 10)
	envCommandLeaseSeconds = "FILEARR_AGENT_COMMAND_LEASE_SECONDS" // picked_up lease; heartbeat = lease/3 (default 300)
)

// startCommandPoller launches the P10-T3 on-demand command poller for the `run`
// daemon: it plain-polls central's per-agent command queue and executes each
// stat_check / rehash_check against local disk, reusing the shared bearer-auth +
// mTLS HTTP client. It returns a done-channel so the daemon waits for a clean
// stop, mirroring startReplication / startPoller.
func startCommandPoller(ctx context.Context, idx *index.Store, certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client) <-chan struct{} {
	poller := commands.NewPoller(commands.Config{
		BaseURL:      centralURL,
		AgentID:      agentID,
		AuthFn:       authProvider(certStore),
		HTTP:         httpClient,
		Executor:     commands.NewExecutor(idx, 0), // 0 => central default size ceiling
		RateProvider: uploadRateProvider(),
		// W6-D3: the inventory runner (real OS host) + the additive capability
		// advertisement it attaches to every poll so central can store what this
		// agent supports.
		Inventory:    inventory.NewRunner(nil, nil),
		Capabilities: inventory.Capabilities(),
		MaxCommands:  envInt(envCommandPollMax, 10),
		Interval:     envDuration(envCommandPollInterval, 60*time.Second),
		LeaseSeconds: envInt(envCommandLeaseSeconds, 300),
		Logger:       newLogger(),
	})
	done := make(chan struct{})
	go func() {
		defer close(done)
		// W6-D3 consumption seam: resolve the group scan_selections policy into the
		// effective scan-root set once at startup (logged + persisted), WITHOUT
		// starting a scan — proves the policy vocabulary resolves end-to-end.
		consumeScanRootSeam(envOr(envDataDir, defaultDataDir()))
		// Run only returns on ctx cancellation (a poll failure backs off, never
		// exits); a non-cancel error is logged but must not crash the daemon.
		if err := poller.Run(ctx); err != nil && ctx.Err() == nil {
			newLogger().Error("command poll loop exited", "err", err)
		}
	}()
	return done
}

// consumeScanRootSeam reads the cached group policy, expands its scan_selections
// into the effective scan-root set via the SHARED pathspec engine, and LOGS +
// PERSISTS the result to <dataDir>/inventory/scan-roots.json. It deliberately does
// NOT start a scan (W6-D3): auto-start from a group policy is a follow-up that must
// coordinate with the scan scheduler/cancellation path. Best-effort throughout — a
// missing cache or unwritable dir is logged at debug and never fatal.
func consumeScanRootSeam(dataDir string) {
	log := newLogger()
	doc, ok, err := agentcfg.NewETagCache(dataDir).Load()
	if err != nil || !ok {
		return // no cached policy yet; nothing to consume
	}
	res := inventory.ExpandScanSelections(pathspec.OSHost(), doc.Policy)
	if res.SelectionsCount == 0 {
		return // no group scan_selections configured
	}
	log.Info("group scan_selections resolved (seam only — no scan started)",
		"selections", res.SelectionsCount, "roots", len(res.Roots), "truncated", res.Truncated)
	dir := filepath.Join(dataDir, "inventory")
	if mkErr := os.MkdirAll(dir, 0o755); mkErr != nil {
		agentlog.Verbose(log, "scan-root seam: cannot create dir", "err", mkErr)
		return
	}
	blob, mErr := json.MarshalIndent(res, "", "  ")
	if mErr != nil {
		return
	}
	if wErr := os.WriteFile(filepath.Join(dir, "scan-roots.json"), blob, 0o644); wErr != nil {
		agentlog.Verbose(log, "scan-root seam: cannot persist", "err", wErr)
	}
}

// uploadRateProvider returns the per-agent staging-upload rate cap (bytes/sec, 0
// = unlimited) read from the cached central policy at each upload start (P10-T4).
// It resolves the data dir the same way the daemon's own default does
// (FILEARR_AGENT_DATA_DIR or the per-user default) — the common deployment; an
// explicit `run -data <dir>` override is not reflected in this lookup, which then
// finds no cache and returns 0 (unlimited). That is a deliberate fail-open: the
// rate cap is a soft throttle, not an integrity or security control, so a missing
// cache must never wedge an upload. A mid-upload policy change is picked up on the
// NEXT upload (the value is re-read per stage_upload, not per chunk).
func uploadRateProvider() func() int64 {
	dataDir := envOr(envDataDir, defaultDataDir())
	return func() int64 {
		pol, ok, err := agentcfg.LoadCachedPolicy(dataDir)
		if err != nil || !ok {
			return 0
		}
		return pol.UploadRateBytesPerSec()
	}
}

// envDuration parses a Go duration from key, falling back to def when unset or
// unparseable/non-positive.
func envDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			return d
		}
	}
	return def
}

// envInt parses a positive int from key, falling back to def otherwise.
func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}
