package main

import (
	"context"
	"path/filepath"
	"time"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/history"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/localapi"
	"github.com/filearr/filearr/agent/internal/query"
)

// startLocalAPI launches the P7-T2 local query transport for the `run` daemon: an
// HTTP/1.1 server over a same-user-only Unix socket (linux/darwin) or Windows
// named pipe, serving the read-only query engine. It returns a done-channel for a
// clean stop.
//
// The server gates on the cached central policy's local_access_enabled key: it
// refuses to start when disabled and stops within one gate interval if a policy
// update disables it (server.Run re-reads the gate). Because the poller persists
// policy.json each cycle, re-reading the cache here honors a flip within one poll
// interval — the "check the cached policy per request" option (the Applier's typed
// view does not yet carry the local-access keys; P7-T4 promotes them).
func startLocalAPI(ctx context.Context, dataDir, socketOverride string, idx *index.Store, hist *history.Store) <-chan struct{} {
	done := make(chan struct{})
	log := newLogger()

	searcher, err := query.NewSearcher(filepath.Join(dataDir, indexDBName))
	if err != nil {
		log.Error("local query API disabled: cannot open read-only index", "err", err)
		close(done)
		return done
	}

	path := socketOverride
	if path == "" {
		path = localapi.DefaultPath(dataDir)
	}
	cache := agentcfg.NewETagCache(dataDir)

	cfg := localapi.Config{
		Path:     path,
		Searcher: searcher,
		Count:    func(ctx context.Context) (int, error) { return countActiveItems(ctx, idx) },
		Policy:   func() localapi.PolicyView { return loadPolicyView(cache) },
		Logger:   log,
	}
	// Only wire history when the store actually opened — a typed-nil interface would
	// panic on Record. The socket surface gets the full History view (record + read).
	if hist != nil {
		cfg.History = hist
	}
	srv := localapi.New(cfg)

	go func() {
		defer close(done)
		defer searcher.Close()
		if err := srv.Run(ctx); err != nil && ctx.Err() == nil {
			log.Error("local query API loop exited", "err", err)
		}
	}()
	return done
}

// countActiveItems reports the count of active (non-tombstoned) items for the
// health probe. Uses the writable store's handle for a read-only COUNT — the
// query surface itself only ever touches the separate read-only connection.
func countActiveItems(ctx context.Context, idx *index.Store) (int, error) {
	var n int
	err := idx.DB().QueryRowContext(ctx,
		`SELECT COUNT(*) FROM items WHERE status = ?`, index.StatusActive).Scan(&n)
	return n, err
}

// loadPolicyView reads the cached central policy and derives the localapi gate
// view, including the P7-T4 freshness (offline-grace) computation and the
// path-scope predicate list. A never-contacted agent (no cache) defaults to local
// access ENABLED, web UI DISABLED, no scope (CLI default-on, brief §5.2). The
// offline-grace default is DefaultOfflineGrace (== defaultReconcileInterval, R4).
func loadPolicyView(cache *agentcfg.ETagCache) localapi.PolicyView {
	doc, ok, err := cache.Load()
	if err != nil || !ok {
		return localapi.PolicyView{LocalAccessEnabled: true}
	}
	ls := doc.LocalSurface(time.Now(), agentcfg.DefaultOfflineGrace)
	pv := localapi.PolicyView{
		LocalAccessEnabled: ls.LocalAccessEnabled,
		WebUIEnabled:       ls.WebUIEnabled, // effective (policy intent AND fresh)
		AuthRequired:       ls.AuthRequired,
		HasVersion:         true,
		Version:            ls.Version,
		Predicates:         ls.Predicates,
		Stale:              ls.Stale,
	}
	if !ls.GraceExpiresAt.IsZero() {
		g := ls.GraceExpiresAt
		pv.GraceExpiresAt = &g
	}
	return pv
}
