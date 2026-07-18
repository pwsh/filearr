package main

import (
	"context"
	"fmt"
	"net/http"

	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
	"github.com/filearr/filearr/agent/internal/reconcile"
)

// newSweeper wires a reconcile Sweeper (protocol client + local store/outbox) for
// the given agent, reusing the replicator's bearer-auth provider and the shared
// mTLS-aware HTTP client (newHTTPClient; nil => client builds its own).
func newSweeper(idx *index.Store, certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client) *reconcile.Sweeper {
	client := reconcile.NewClient(reconcile.ClientConfig{
		BaseURL: centralURL,
		AgentID: agentID,
		AuthFn:  authProvider(certStore),
		HTTP:    httpClient,
		Logger:  newLogger(),
	})
	return reconcile.NewSweeper(idx, outbox.New(idx.DB()), client, newLogger())
}

// runReconcile implements the one-shot `filearr-agent reconcile` (trigger d): a
// manual full-manifest sweep of every configured root, printing per-root counters.
func runReconcile(args []string) error {
	fs := newFlagSet("reconcile")
	cfg := bindCommonFlags(fs)
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

	idx, err := openIndex(cfg.DataDir)
	if err != nil {
		return err
	}
	defer idx.Close()

	ctx, cancel := signalContext()
	defer cancel()

	httpClient, err := newHTTPClient(certStore, centralURL)
	if err != nil {
		return err
	}
	sweeper := newSweeper(idx, certStore, centralURL, st.AgentID, httpClient)
	res, err := sweeper.Sweep(ctx, reconcile.Options{})
	printSweep(res)
	return err
}

// printSweep renders a SweepResult to stdout for the manual subcommand.
func printSweep(res reconcile.SweepResult) {
	if len(res.Roots) == 0 {
		fmt.Println("reconcile: no roots configured")
	}
	for _, rr := range res.Roots {
		switch {
		case rr.Err != nil:
			fmt.Printf("reconcile %s: error: %v\n", rr.LibraryRef, rr.Err)
		case rr.Matched:
			fmt.Printf("reconcile %s: match (rows=%d)\n", rr.LibraryRef, rr.RowCount)
		default:
			fmt.Printf("reconcile %s: reconciled rows=%d reset=%v %s\n",
				rr.LibraryRef, rr.RowCount, rr.Reset, rr.Finish.SortedCounters())
		}
	}
	if res.Reset {
		fmt.Printf("reconcile: outbox superseded (rebuilt=%v marked=%d)\n", res.Rebuilt, res.OutboxMarked)
	}
}

// startSupervisor builds the reconcile trigger Supervisor for the `run` daemon
// and launches its loop. It returns the Supervisor (registered as the
// replicator's Observer for triggers b/c) and a done-channel for a clean stop.
func startSupervisor(ctx context.Context, idx *index.Store, certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client) (*reconcile.Supervisor, <-chan struct{}) {
	sweeper := newSweeper(idx, certStore, centralURL, agentID, httpClient)
	interval := reconcileInterval()
	sup := reconcile.NewSupervisor(sweeper.Sweep, interval, interval, newLogger())
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = sup.Run(ctx)
	}()
	return sup, done
}
