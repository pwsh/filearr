package main

import (
	"context"
	"errors"
	"fmt"
	"os"

	"github.com/filearr/filearr/agent/internal/enroll"
)

// runDaemon loads the persisted cert store and runs the long-lived daemon:
// certificate renewal + replication drain + reconcile supervisor + policy poller
// + the P7-T2 local query API. Flags/behavior are byte-compatible with the
// pre-P7-T3 dispatch (it runs under urfave's SkipFlagParsing with its own stdlib
// flag.FlagSet, including the -socket override).
func runDaemon(args []string) error {
	fs := newFlagSet("run")
	cfg := bindCommonFlags(fs)
	socket := fs.String("socket", "", "override the local query API socket path (unix) / named-pipe name (windows); default is per-user")
	webAddr := fs.String("web-addr", envOr(envWebUIAddr, ""), "local web UI loopback bind address (host:port); default 127.0.0.1:8686 when the policy enables the web UI")
	if err := fs.Parse(args); err != nil {
		return err
	}

	store := enroll.NewCertStore(cfg.DataDir)
	// Load + validate the persisted identity up front so misconfiguration fails
	// fast rather than after the first renewal timer fires.
	id, err := store.Load()
	if err != nil {
		return fmt.Errorf("no enrolled identity in %s (run `filearr-agent enroll` first): %w", cfg.DataDir, err)
	}

	ctx, cancel := signalContext()
	defer cancel()

	renewer := &enroll.Renewer{
		Store:      store,
		CAURL:      id.State.CAURL,
		RootSHA256: id.State.CARootSHA256,
		Logger:     newLogger(),
	}

	// The replication drain loop runs alongside the renewer under the same ctx.
	// A shutdown cancels both; the drain finishes or durably-unsends its in-flight
	// batch (never half-marked). The local index is opened here even absent a
	// prior scan (an empty outbox simply drains to nothing).
	idx, err := openIndex(cfg.DataDir)
	if err != nil {
		return err
	}
	defer idx.Close()

	// The P7-T6 local query frecency store: a SEPARATE database file from the index
	// so it can never ride the outbox/replication path (search history stays local,
	// research §6). Opened once and shared by the socket + web UI query surfaces. A
	// failure here is non-fatal — the query surfaces run without history recording.
	hist, herr := openHistory(cfg.DataDir)
	if herr != nil {
		newLogger().Error("local query history disabled: cannot open history store", "err", herr)
		hist = nil
	} else {
		defer hist.Close()
	}

	// The ONE mTLS-aware HTTP client shared by replication, reconcile, and the
	// policy poller (P5-T6): presents the enrolled client cert to central over
	// https. Built once and reused across all three loops.
	httpClient, err := newHTTPClient(store, id.State.CentralURL)
	if err != nil {
		return err
	}

	// The reconcile Supervisor owns the full-manifest sweep triggers. It is the
	// replicator's Observer (triggers b/c: reconnect-after-outage and the
	// cursor-dead-end reset) and runs the slow interval tick (trigger a).
	sup, supDone := startSupervisor(ctx, idx, store, id.State.CentralURL, id.State.AgentID, httpClient)
	replDone := startReplication(ctx, idx, store, id.State.CentralURL, id.State.AgentID, sup, httpClient)
	// The policy poller runs under the same ctx: it applies the last-known policy
	// offline-first, then polls central (ETag-conditional) and live-updates the
	// reconcile cadence + persists scan/watch settings for the scan path to honor.
	pollDone := startPoller(ctx, cfg.DataDir, store, id.State.CentralURL, id.State.AgentID, sup, httpClient)
	// The P7-T2 local query API (same-user Unix socket / Windows named pipe) serves
	// the read-only query engine to a same-machine CLI. It gates on the cached
	// policy's local_access_enabled key (default-on for a never-contacted agent).
	localDone := startLocalAPI(ctx, cfg.DataDir, *socket, idx, hist)
	// The P7-T5 local web UI (read-only browser search surface, loopback TCP only)
	// serves the same read-only query engine to a same-machine browser. It gates on
	// the cached policy's EFFECTIVE web-UI capability (web_ui_enabled AND fresh);
	// off by default for a never-contacted agent, and it fails closed when the
	// policy goes stale past grace (R4) — the socket transport above is unaffected.
	webDone := startWebUI(ctx, cfg.DataDir, *webAddr, idx, hist)
	// The P10-T3 on-demand command poller (stat_check / rehash_check verification)
	// drains central's per-agent command queue under the same ctx + shared client.
	cmdDone := startCommandPoller(ctx, idx, store, id.State.CentralURL, id.State.AgentID, httpClient)
	// The P12-T13 thumbnail pass: a low-priority background walk that generates
	// grid+preview thumbnails for locally-hosted items and pushes them to central's
	// agent-plane small-blob endpoint (write-if-absent, content-addressed). Shares
	// the mTLS/bearer HTTP client; disabled with FILEARR_AGENT_THUMBS_ENABLED=false.
	thumbDone := startThumbnailer(ctx, idx, store, id.State.CentralURL, id.State.AgentID, httpClient)
	// The P5-T7 self-updater: runs the crash-loop boot check (may roll back +
	// re-exec), then polls central's signed update manifest and A/B-swaps a newer,
	// signature-verified binary. Shares the mTLS/bearer HTTP client.
	updDone := startUpdater(ctx, cfg.DataDir, store, id.State.CentralURL, id.State.AgentID, httpClient)

	fmt.Printf("filearr-agent %s running: agent_id=%s central=%s\n",
		Version, id.State.AgentID, id.State.CentralURL)
	fmt.Printf("renewal daemon + replication drain + reconcile supervisor + policy poller + local query API + local web UI started (cert valid until %s)\n", id.Leaf.NotAfter.Format("2006-01-02T15:04:05Z07:00"))

	// Run blocks until ctx is cancelled (SIGINT/SIGTERM) and then returns
	// ctx.Err(); treat that as a clean shutdown.
	err = renewer.Run(ctx)
	<-replDone  // let the drain loop unwind cleanly before returning
	<-supDone   // and the supervisor loop
	<-pollDone  // and the policy poll loop
	<-localDone // and the local query API
	<-webDone   // and the local web UI
	<-cmdDone   // and the command poll loop
	<-thumbDone // and the thumbnail generation pass
	<-updDone   // and the self-updater poll loop
	if errors.Is(err, context.Canceled) {
		fmt.Fprintln(os.Stderr, "shutting down")
		return nil
	}
	return err
}
