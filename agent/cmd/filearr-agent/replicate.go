package main

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"path/filepath"

	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/history"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
)

// envAuthFingerprint is the interim bearer-token fallback (see authProvider).
const envAuthFingerprint = "FILEARR_AGENT_AUTH_FINGERPRINT"

// authProvider returns the bearer-token source for the replication client.
//
// Interim pre-mTLS scheme (docs/ops/agents.md §6): central authenticates an agent
// by its bound cert fingerprint carried as a bearer token. We use the on-disk
// leaf's fingerprint when the cert store loads, else fall back to
// FILEARR_AGENT_AUTH_FINGERPRINT. CAVEAT: certificate renewal issues a NEW leaf
// with a NEW fingerprint, while central still holds the fingerprint bound at
// enrollment — so after the first renewal the leaf-derived value can drift and
// central may 401. Until P5-T6 replaces this with the verified mTLS client
// identity, pin the enrollment fingerprint via the env var on a host whose cert
// has rotated. The token is read per-request so such a change is picked up live.
func authProvider(store *enroll.CertStore) func() string {
	return func() string {
		if id, err := store.Load(); err == nil && id.Leaf != nil {
			return enroll.CertFingerprint(id.Leaf)
		}
		return os.Getenv(envAuthFingerprint)
	}
}

// openIndex opens the local catalog under the data dir (shared by scan/push/run).
func openIndex(dataDir string) (*index.Store, error) {
	store, err := index.Open(filepath.Join(dataDir, indexDBName))
	if err != nil {
		return nil, fmt.Errorf("open local index: %w", err)
	}
	return store, nil
}

// openHistory opens the P7-T6 local query frecency store — a SEPARATE database
// file from the index (historyDBName), the architectural isolation that keeps
// search history off the outbox/replication path (internal/history).
func openHistory(dataDir string) (*history.Store, error) {
	store, err := history.Open(filepath.Join(dataDir, historyDBName))
	if err != nil {
		return nil, fmt.Errorf("open local history: %w", err)
	}
	return store, nil
}

// newReplicator wires an outbox replicator against central for the given agent.
// observer (optional, nil for one-shot push) receives drain-health signals so the
// run daemon's reconcile Supervisor can trigger a sweep. httpClient is the shared
// mTLS-aware client (newHTTPClient); nil lets the replicator build its own.
func newReplicator(idx *index.Store, certStore *enroll.CertStore, centralURL, agentID string, observer outbox.Observer, httpClient *http.Client) *outbox.Replicator {
	return outbox.NewReplicator(outbox.New(idx.DB()), outbox.Config{
		BaseURL:  centralURL,
		AgentID:  agentID,
		AuthFn:   authProvider(certStore),
		HTTP:     httpClient,
		Logger:   newLogger(),
		Observer: observer,
	})
}

// runPush implements the one-shot `filearr-agent push`: drain the outbox until it
// is empty or a flush errors, then print the counters. Handy for scripting and
// testing a backlog without the long-running daemon.
func runPush(args []string) error {
	fs := newFlagSet("push")
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
	rep := newReplicator(idx, certStore, centralURL, st.AgentID, nil, httpClient)
	counters, err := rep.Push(ctx)
	fmt.Printf("push: batches=%d rows_sent=%d applied=%d upserted=%d tombstoned=%d central_last_seq=%d\n",
		counters.Batches, counters.Rows, counters.Applied, counters.Upserted, counters.Tombstoned, counters.LastSeq)
	if err != nil {
		return err
	}
	return nil
}

// startReplication launches the drain loop for the `run` daemon. It returns the
// replicator's goroutine done-channel so the caller waits for a clean stop.
func startReplication(ctx context.Context, idx *index.Store, certStore *enroll.CertStore, centralURL, agentID string, observer outbox.Observer, httpClient *http.Client) <-chan struct{} {
	rep := newReplicator(idx, certStore, centralURL, agentID, observer, httpClient)
	done := make(chan struct{})
	go func() {
		defer close(done)
		// Run only returns on ctx cancellation (a flush failure backs off, never
		// exits); a non-cancel error is logged but must not crash the daemon.
		if err := rep.Run(ctx); err != nil && ctx.Err() == nil {
			newLogger().Error("replication drain loop exited", "err", err)
		}
	}()
	return done
}
