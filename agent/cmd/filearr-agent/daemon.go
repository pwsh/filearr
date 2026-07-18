package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"

	"github.com/kardianos/service"

	"github.com/filearr/filearr/agent/internal/agentlog"
	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/install"
)

// runDaemon runs the long-lived daemon (certificate renewal + replication drain
// + reconcile supervisor + policy poller + local query API + web UI + command
// poller + thumbnailer + self-updater) UNDER kardianos service management.
//
// Wrapping the existing daemon logic in a service.Interface (rather than forking
// a second copy) is what makes `filearr-agent run` behave correctly in all three
// contexts from one code path: an interactive terminal (kardianos handles
// SIGINT/SIGTERM), a systemd/launchd unit, and the Windows SCM (which requires
// the process to answer the service control dispatcher — a bare console loop
// would be killed with "did not respond in a timely fashion"). The daemon body
// is unchanged; only its lifecycle owner is.
func runDaemon(args []string) error {
	fs := newFlagSet("run")
	cfg := bindCommonFlags(fs)
	socket := fs.String("socket", "", "override the local query API socket path (unix) / named-pipe name (windows); default is per-user")
	webAddr := fs.String("web-addr", envOr(envWebUIAddr, ""), "local web UI loopback bind address (host:port); default 127.0.0.1:8686 when the policy enables the web UI")
	if err := fs.Parse(args); err != nil {
		return err
	}

	// Fail fast BEFORE constructing the service so a misconfigured/unenrolled host
	// reports the same clear error whether launched interactively or by a service
	// manager (rather than a service manager reporting an opaque start failure).
	store := enroll.NewCertStore(cfg.DataDir)
	if _, err := store.Load(); err != nil {
		return fmt.Errorf("no enrolled identity in %s (run `filearr-agent enroll` first): %w", cfg.DataDir, err)
	}

	prog := &daemonProgram{cfg: cfg, socket: *socket, webAddr: *webAddr, log: newLogger()}
	svc, err := service.New(prog, install.RunServiceConfig())
	if err != nil {
		return fmt.Errorf("wire service manager: %w", err)
	}
	// Run blocks until the service manager (or an interactive interrupt) stops us;
	// it returns Start's error (fail-fast) or Stop's error (the daemon's run err).
	return svc.Run()
}

// daemonProgram adapts the daemon to kardianos service.Interface. Start wires and
// launches every loop and returns immediately (kardianos requires a non-blocking
// Start); Stop cancels the shared context and waits for a clean unwind.
type daemonProgram struct {
	cfg     *config
	socket  string
	webAddr string
	log     *slog.Logger

	cancel context.CancelFunc
	done   chan struct{}
	runErr error
}

// Start loads the identity, opens the local stores, and launches all daemon
// loops under one cancellable context. It must not block: the renewer's blocking
// Run + the wait-for-unwind live in a goroutine whose completion closes p.done.
func (p *daemonProgram) Start(s service.Service) error {
	store := enroll.NewCertStore(p.cfg.DataDir)
	id, err := store.Load()
	if err != nil {
		return fmt.Errorf("no enrolled identity in %s (run `filearr-agent enroll` first): %w", p.cfg.DataDir, err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	p.cancel = cancel

	// The local index is opened even absent a prior scan (an empty outbox simply
	// drains to nothing).
	idx, err := openIndex(p.cfg.DataDir)
	if err != nil {
		cancel()
		return err
	}

	// The P7-T6 local query frecency store is a SEPARATE database from the index
	// (search history stays local, research §6). A failure is non-fatal.
	hist, herr := openHistory(p.cfg.DataDir)
	if herr != nil {
		p.log.Error("local query history disabled: cannot open history store", "err", herr)
		hist = nil
	}

	httpClient, err := newHTTPClient(store, id.State.CentralURL)
	if err != nil {
		cancel()
		_ = idx.Close()
		if hist != nil {
			_ = hist.Close()
		}
		return err
	}

	// service.Interactive() is false only under an OS service manager. That is the
	// signal the self-updater needs to prefer clean-exit-for-restart over
	// self-re-exec after an A/B swap (a service manager races a re-exec).
	serviceManaged := !service.Interactive()
	agentlog.Verbose(p.log, "daemon starting",
		"agent_id", id.State.AgentID, "central", id.State.CentralURL, "service_managed", serviceManaged)

	renewer := &enroll.Renewer{
		Store:      store,
		CAURL:      id.State.CAURL,
		RootSHA256: id.State.CARootSHA256,
		Logger:     p.log,
	}

	sup, supDone := startSupervisor(ctx, idx, store, id.State.CentralURL, id.State.AgentID, httpClient)
	replDone := startReplication(ctx, idx, store, id.State.CentralURL, id.State.AgentID, sup, httpClient)
	pollDone := startPoller(ctx, p.cfg.DataDir, store, id.State.CentralURL, id.State.AgentID, sup, httpClient)
	localDone := startLocalAPI(ctx, p.cfg.DataDir, p.socket, idx, hist)
	webDone := startWebUI(ctx, p.cfg.DataDir, p.webAddr, idx, hist)
	cmdDone := startCommandPoller(ctx, idx, store, id.State.CentralURL, id.State.AgentID, httpClient)
	thumbDone := startThumbnailer(ctx, idx, store, id.State.CentralURL, id.State.AgentID, httpClient)
	updDone := startUpdater(ctx, p.cfg.DataDir, store, id.State.CentralURL, id.State.AgentID, httpClient, serviceManaged)

	fmt.Printf("filearr-agent %s running: agent_id=%s central=%s\n",
		Version, id.State.AgentID, id.State.CentralURL)
	fmt.Printf("renewal daemon + replication drain + reconcile supervisor + policy poller + local query API + local web UI started (cert valid until %s)\n", id.Leaf.NotAfter.Format("2006-01-02T15:04:05Z07:00"))

	p.done = make(chan struct{})
	go func() {
		defer close(p.done)
		defer func() {
			_ = idx.Close()
			if hist != nil {
				_ = hist.Close()
			}
		}()
		// Run blocks until ctx is cancelled (Stop / interrupt) and returns
		// ctx.Err(); let every sibling loop unwind cleanly before returning.
		err := renewer.Run(ctx)
		<-replDone
		<-supDone
		<-pollDone
		<-localDone
		<-webDone
		<-cmdDone
		<-thumbDone
		<-updDone
		if err != nil && !errors.Is(err, context.Canceled) {
			p.runErr = err
		}
	}()
	return nil
}

// Stop cancels the daemon context and waits for a clean unwind. kardianos calls
// it on service stop or an interactive interrupt.
func (p *daemonProgram) Stop(s service.Service) error {
	if p.cancel != nil {
		p.cancel()
	}
	if p.done != nil {
		<-p.done
	}
	if p.runErr != nil {
		return p.runErr
	}
	fmt.Fprintln(os.Stderr, "shutting down")
	return nil
}
