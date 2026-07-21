package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/reconcile"
	"github.com/filearr/filearr/agent/internal/taxonomy"
)

// daemonApplier is the `run` daemon's Applier: it live-applies the honored policy
// keys to the running components. Scan-relevant keys (presets/globs/content
// ceiling) and watch_mode are consumed by reading the persisted policy.json at
// scan time (the poller persists them), so the only live wiring here is the
// reconcile-supervisor cadence.
type daemonApplier struct {
	sup *reconcile.Supervisor
	log *slog.Logger
}

func (a *daemonApplier) ApplyPolicy(p agentcfg.Policy) error {
	if d, ok := p.ReconcileInterval(); ok {
		a.sup.SetInterval(d)
		a.log.Info("policy applied: reconcile interval", "interval", d.String())
	}
	if allowed, set := p.WatchAllowed(); set {
		a.log.Info("policy applied: watch_mode", "watch_mode", allowed)
	}
	return nil
}

// newPolicyClient builds the policy poll client against central, reusing the
// replicator's bearer-auth provider (interim cert-fingerprint scheme) and the
// shared mTLS-aware HTTP client (newHTTPClient; nil => client builds its own).
func newPolicyClient(certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client) *agentcfg.PolicyClient {
	return agentcfg.NewPolicyClient(agentcfg.ClientConfig{
		BaseURL: centralURL,
		AgentID: agentID,
		AuthFn:  authProvider(certStore),
		HTTP:    httpClient,
		Logger:  newLogger(),
	})
}

// startPoller launches the policy poll loop for the `run` daemon alongside the
// renewer/replicator/supervisor. It returns a done-channel for a clean stop.
//
// W8-E: the poller also keeps the process-shared taxonomy cache fresh. After
// every successful poll it version-gates a taxonomy fetch off the policy's
// taxonomy_version, so an operator taxonomy edit (which bumps the policy ETag)
// propagates the compact taxonomy to <dataDir>/taxonomy.json — the same file the
// `scan` path reads. Refresh runs in a detached goroutine so the ~1271-entry
// fetch never blocks the poll loop.
func startPoller(ctx context.Context, dataDir string, certStore *enroll.CertStore, centralURL, agentID string, sup *reconcile.Supervisor, httpClient *http.Client) <-chan struct{} {
	taxCache := taxonomy.NewCache(dataDir, newLogger())
	taxClient := taxonomy.NewClient(taxonomy.ClientConfig{
		BaseURL: centralURL,
		AgentID: agentID,
		AuthFn:  authProvider(certStore),
		HTTP:    httpClient,
		Logger:  newLogger(),
	})
	poller := agentcfg.NewPoller(agentcfg.PollerConfig{
		Client:     newPolicyClient(certStore, centralURL, agentID, httpClient),
		Cache:      agentcfg.NewETagCache(dataDir),
		Applier:    &daemonApplier{sup: sup, log: newLogger()},
		Logger:     newLogger(),
		AfterFetch: taxonomyRefreshHook(taxCache, taxClient, newLogger()),
	})
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := poller.Run(ctx); err != nil && ctx.Err() == nil {
			newLogger().Error("policy poll loop exited", "err", err)
		}
	}()
	return done
}

// taxonomyRefreshHook returns a poller AfterFetch callback that version-gates a
// taxonomy refresh off the freshly-fetched policy's taxonomy_version (W8-E). The
// fetch runs in a detached goroutine so it never blocks the poll loop; a nil
// taxonomy_version (older central / never set) is a no-op.
func taxonomyRefreshHook(cache *taxonomy.Cache, client *taxonomy.Client, log *slog.Logger) func(context.Context, agentcfg.PolicyDoc) {
	return func(ctx context.Context, doc agentcfg.PolicyDoc) {
		pol, err := doc.Parsed()
		if err != nil {
			return
		}
		want := pol.TaxonomyVersionValue()
		if want <= cache.Version() {
			return
		}
		go func() {
			if err := cache.Refresh(ctx, client, want); err != nil {
				log.Warn("taxonomy refresh failed; keeping last-known taxonomy", "want", want, "err", err)
			}
		}()
	}
}

// runPolicy implements `filearr-agent policy [--fetch]`: without --fetch it
// prints the cached policy; with --fetch it does a one-shot poll+apply+persist
// against central (scripting/testing).
func runPolicy(args []string) error {
	fs := newFlagSet("policy")
	cfg := bindCommonFlags(fs)
	fetch := fs.Bool("fetch", false, "do a one-shot poll+apply against central (else print the cached policy)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	cache := agentcfg.NewETagCache(cfg.DataDir)
	if !*fetch {
		doc, ok, err := cache.Load()
		if err != nil {
			return err
		}
		if !ok {
			fmt.Printf("no cached policy at %s (run `filearr-agent policy --fetch` or start the daemon)\n", cache.Path())
			return nil
		}
		printPolicyDoc(doc)
		return nil
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
	// A one-shot CLI has no live daemon to reconfigure; NoopApplier still persists
	// the fetched policy so a subsequent `scan` honors central's scan settings.
	poller := agentcfg.NewPoller(agentcfg.PollerConfig{
		Client:  newPolicyClient(certStore, centralURL, st.AgentID, httpClient),
		Cache:   cache,
		Applier: agentcfg.NoopApplier{},
		Logger:  newLogger(),
	})

	ctx, cancel := signalContext()
	defer cancel()

	doc, outcome, err := poller.PollOnce(ctx)
	if err != nil {
		return err
	}
	printPolicyDoc(doc)
	switch outcome {
	case agentcfg.OutcomeApplied:
		fmt.Println("result: fetched new scope/version and applied")
	case agentcfg.OutcomeNotModified:
		fmt.Println("result: not modified (304, cache already current)")
	default: // OutcomeUnchanged
		fmt.Println("result: fetched, identity unchanged (no apply)")
	}
	return nil
}

// printPolicyDoc renders a cached/fetched policy document for the CLI.
func printPolicyDoc(doc agentcfg.PolicyDoc) {
	fetched := "never"
	if !doc.FetchedAt.IsZero() {
		fetched = doc.FetchedAt.Format(time.RFC3339)
	}
	fmt.Printf("scope=%s version=%d applied_version=%d etag=%s fetched_at=%s\n",
		doc.Scope, doc.Version, doc.AppliedVersion, doc.ETag, fetched)
	keys := doc.PolicyKeys()
	if len(keys) == 0 {
		fmt.Println("policy: (empty — defaults apply)")
		return
	}
	fmt.Printf("policy keys: %s\n", strings.Join(keys, ", "))
}
