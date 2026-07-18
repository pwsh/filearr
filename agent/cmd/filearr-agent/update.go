package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/update"
)

// Updater env fallbacks (a background daemon concern, no operator flags plumbed
// for the poll loop — mirrors the command poller).
const envUpdatePollInterval = "FILEARR_AGENT_UPDATE_POLL_INTERVAL" // Go duration (default 6h)

// newUpdater builds the self-updater for the given agent, reusing the shared
// bearer-auth provider (interim cert-fingerprint scheme) and the shared
// mTLS-aware HTTP client. The pinned release-signing public key is baked in at
// build time (update.PublicKeyBase64 via -ldflags); an unpinned build refuses
// every update (fail-closed), logged once at startup.
func newUpdater(certStore *enroll.CertStore, dataDir, centralURL, agentID string, httpClient *http.Client, serviceManaged bool) *update.Updater {
	log := newLogger()
	pub, err := update.PinnedKey()
	if err != nil {
		log.Warn("agent updater: no valid pinned signing key; self-updates are DISABLED (build with -ldflags -X ...update.PublicKeyBase64=<key>)", "err", err)
	}
	return update.New(update.Config{
		BaseURL:        centralURL,
		AgentID:        agentID,
		AuthFn:         authProvider(certStore),
		HTTP:           httpClient,
		DataDir:        dataDir,
		CurrentVersion: Version,
		PublicKey:      pub,
		Interval:       envDuration(envUpdatePollInterval, 6*time.Hour),
		Logger:         log,
		// Under a service manager, prefer a clean restart-exit after an A/B swap
		// over self-re-exec: the manager owns process lifecycle and would race a
		// re-exec (and could end up running two instances). Interactive `run` keeps
		// the historic self-re-exec path.
		ServiceManaged: serviceManaged,
	})
}

// startUpdater launches the P5-T7 self-updater for the `run` daemon. It FIRST
// runs the crash-loop boot check synchronously (which may restore the previous
// binary and re-exec+exit on a failed update), then launches the health window
// (if a swapped-in update is on trial this boot) and the long-interval poll loop.
// It returns a done-channel so the daemon waits for a clean stop, mirroring
// startCommandPoller / startReplication.
func startUpdater(ctx context.Context, dataDir string, certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client, serviceManaged bool) <-chan struct{} {
	upd := newUpdater(certStore, dataDir, centralURL, agentID, httpClient, serviceManaged)
	log := newLogger()

	// Boot check before any loop: on an exhausted trial this restores + re-execs
	// the previous binary and never returns. On a live trial it returns
	// healthPending=true (the counter was incremented + persisted).
	healthPending, err := upd.BootCheck(ctx)
	if err != nil {
		log.Error("update boot check failed; continuing on current binary", "err", err)
	}

	done := make(chan struct{})
	go func() {
		defer close(done)
		if healthPending {
			// Prove this freshly-swapped binary healthy (survives the window),
			// then clear the boot counter + delete the .old binary + confirm.
			go upd.RunHealthWindow(ctx)
		}
		if err := upd.Run(ctx); err != nil && ctx.Err() == nil {
			log.Error("update poll loop exited", "err", err)
		}
	}()
	return done
}

// runUpdate implements the one-shot `filearr-agent update [--check]`: check for a
// signed update now and (unless --check) apply it, honoring the same verify path.
func runUpdate(args []string) error {
	fs := newFlagSet("update")
	cfg := bindCommonFlags(fs)
	checkOnly := fs.Bool("check", false, "print the available version without applying it")
	if err := fs.Parse(args); err != nil {
		return err
	}

	certStore := enroll.NewCertStore(cfg.DataDir)
	st, err := certStore.LoadState()
	if err != nil {
		return fmt.Errorf("no enrolled identity in %s (run `filearr-agent enroll` first): %w", cfg.DataDir, err)
	}
	centralURL := cfg.CentralURL
	if centralURL == "" {
		centralURL = st.CentralURL
	}
	if centralURL == "" {
		return fmt.Errorf("central URL is required (-central, %s, or state.json)", envCentralURL)
	}

	httpClient, err := newHTTPClient(certStore, centralURL)
	if err != nil {
		return err
	}

	ctx, cancel := signalContext()
	defer cancel()

	// The one-shot `update` command always runs interactively (an operator typed
	// it), so it uses the self-re-exec path regardless of any service.
	upd := newUpdater(certStore, cfg.DataDir, centralURL, st.AgentID, httpClient, false)
	m, a, ok, err := upd.CheckForUpdate(ctx)
	if err != nil {
		return err
	}
	if !ok {
		if m != nil {
			fmt.Printf("up to date (running %s; central offers %s, not newer)\n", Version, m.Version)
		} else {
			fmt.Printf("up to date (running %s)\n", Version)
		}
		return nil
	}
	if *checkOnly {
		fmt.Printf("update available: %s -> %s (%s/%s, %s)\n", Version, m.Version, a.Platform, a.Arch, a.URL)
		return nil
	}
	fmt.Printf("applying update %s -> %s ...\n", Version, m.Version)
	// ApplyUpdate re-execs + exits on success; a return means a pre-swap failure.
	if err := upd.ApplyUpdate(ctx, m, a); err != nil {
		return err
	}
	os.Exit(0)
	return nil
}
