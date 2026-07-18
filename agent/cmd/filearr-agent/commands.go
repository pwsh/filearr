package main

import (
	"context"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/filearr/filearr/agent/internal/commands"
	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/index"
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
		MaxCommands:  envInt(envCommandPollMax, 10),
		Interval:     envDuration(envCommandPollInterval, 60*time.Second),
		LeaseSeconds: envInt(envCommandLeaseSeconds, 300),
		Logger:       newLogger(),
	})
	done := make(chan struct{})
	go func() {
		defer close(done)
		// Run only returns on ctx cancellation (a poll failure backs off, never
		// exits); a non-cancel error is logged but must not crash the daemon.
		if err := poller.Run(ctx); err != nil && ctx.Err() == nil {
			newLogger().Error("command poll loop exited", "err", err)
		}
	}()
	return done
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
