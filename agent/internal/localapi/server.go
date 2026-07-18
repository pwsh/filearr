package localapi

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"time"

	"github.com/filearr/filearr/agent/internal/history"
	"github.com/filearr/filearr/agent/internal/query"
)

const (
	// maxLimit is the query result-window ceiling (matches the contract's
	// QueryRequest.limit le=1000).
	maxLimit = 1000
	// defaultLimit applies when the client omits limit (0). Matches the contract
	// default (QueryRequest.limit default=100).
	defaultLimit = 100
	// defaultGateInterval is how often Run re-reads the cached-policy gate to
	// honor a local_access_enabled flip. Kept below the poll floor (MinPollInterval
	// = 60s) so a disable is honored "within one poll interval" (P7-T4 accept).
	defaultGateInterval = 30 * time.Second
	// maxRequestBytes bounds a query request body (defensive; a query string is tiny).
	maxRequestBytes = 1 << 20
)

// Searcher is the read-only query engine served over the transport — satisfied by
// *query.Searcher (P7-T1). The interface keeps the server testable with a fake.
// SearchScoped applies the policy-cached path-scope allow-list server-side; scope
// is NEVER taken from the client request (research §4.4).
type Searcher interface {
	SearchScoped(ctx context.Context, raw string, includeSidecars bool, limit int, scope []string) ([]query.Result, error)
}

// PolicyView is the localapi-relevant slice of the agent's cached central policy.
// P7-T2 reads these keys defensively from the raw policy body (see
// PolicyViewFromRaw); P7-T4 will formalize them as typed config.Policy fields and
// own the offline-grace/stale computation (left zero-valued here). A
// never-contacted agent defaults LocalAccessEnabled=true (CLI enabled by default,
// brief §5.2) and WebUIEnabled=false.
type PolicyView struct {
	LocalAccessEnabled bool
	WebUIEnabled       bool // P7-T4: the EFFECTIVE capability (policy intent AND fresh)
	AuthRequired       bool
	HasVersion         bool
	Version            int
	// Predicates is the flattened path-scope allow-list from the CACHED policy
	// (P7-T4). Applied to every query as an OR-combined rel_path GLOB; empty =
	// unrestricted. Stale caches keep enforcing the last-known predicates (never
	// widened to unrestricted — research §4.4).
	Predicates     []string
	Stale          bool       // cached policy is past its offline-grace window (P7-T4)
	GraceExpiresAt *time.Time // when the cache goes stale (P7-T4)
}

// Recorder records a successful query into the LOCAL-ONLY frecency store (P7-T6)
// — satisfied by *history.Store. It is a write-only view: the web UI is given
// only this interface so it structurally cannot READ history (history read access
// is reserved to the socket API surface, per the task).
type Recorder interface {
	Record(ctx context.Context, raw string) error
}

// History is the socket server's history view: record successful queries AND read
// the top frecency entries for the ranking surface. Satisfied by *history.Store.
type History interface {
	Recorder
	Top(ctx context.Context, limit int) ([]history.Entry, error)
}

// Config wires a Server. Path is the socket path / pipe name (use DefaultPath).
type Config struct {
	Path     string
	Searcher Searcher
	// History is the local-only query frecency store (P7-T6). When non-nil, a
	// successful query is recorded and GET /v1/history serves suggestions. nil
	// disables both (recording and the endpoint) — history is strictly local and
	// never leaves the machine (see internal/history).
	History History
	// Count reports the local index item count for GET /v1/health; an error marks
	// the index not-ready (status "degraded").
	Count func(ctx context.Context) (int, error)
	// Policy returns a live snapshot of the cached-policy gate. Called on each
	// gate tick and each request; nil defaults to LocalAccessEnabled=true.
	Policy func() PolicyView
	// GateInterval overrides how often Run re-checks the policy gate (test seam).
	GateInterval time.Duration
	Logger       *slog.Logger
	// Now is a clock seam for health timestamps; defaults to time.Now.
	Now func() time.Time
}

// Server serves the read-only query engine over the local transport.
type Server struct {
	cfg          Config
	log          *slog.Logger
	now          func() time.Time
	policy       func() PolicyView
	gateInterval time.Duration
}

// New constructs a Server, applying defaults for the optional seams.
func New(cfg Config) *Server {
	s := &Server{cfg: cfg, log: cfg.Logger, now: cfg.Now, policy: cfg.Policy, gateInterval: cfg.GateInterval}
	if s.log == nil {
		s.log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if s.now == nil {
		s.now = time.Now
	}
	if s.policy == nil {
		s.policy = func() PolicyView { return PolicyView{LocalAccessEnabled: true} }
	}
	if s.gateInterval <= 0 {
		s.gateInterval = defaultGateInterval
	}
	return s
}

// Handler returns the read-only HTTP mux. Only two routes are registered, both
// non-mutating: GET /v1/health and POST /v1/query (POST carries the query body,
// it does not mutate — the read-only DB handle is the storage-layer guarantee,
// brief §3.4). A method backstop returns 405 for anything else. Exposed so tests
// can exercise the contract over httptest without the platform transport.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/query", s.handleQuery)
	mux.HandleFunc("GET /v1/health", s.handleHealth)
	mux.HandleFunc("GET /v1/history", s.handleHistory)
	return methodBackstop(mux)
}

// methodBackstop rejects any non-GET/HEAD/POST method before it can reach a
// handler — a belt-and-suspenders guard so a future route can never silently
// accept a mutating verb (brief §3.4 layered read-only enforcement).
func methodBackstop(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet, http.MethodHead, http.MethodPost:
			next.ServeHTTP(w, r)
		default:
			writeError(w, http.StatusMethodNotAllowed, errorBody{Error: "method not allowed", Code: "method_not_allowed"})
		}
	})
}

// Run owns the transport lifecycle. It listens+serves while the cached policy has
// LocalAccessEnabled; it refuses to start (logged, non-fatal) when disabled, and
// stops serving within one gate interval if a policy update flips it off — then
// resumes if re-enabled. Returns ctx.Err() on shutdown.
func (s *Server) Run(ctx context.Context) error {
	gate := time.NewTicker(s.gateInterval)
	defer gate.Stop()

	var srv *http.Server
	loggedDisabled := false
	stop := func(reason string) {
		if srv != nil {
			s.log.Info("stopping local query API", "reason", reason, "path", s.cfg.Path)
			_ = srv.Close()
			srv = nil
		}
	}
	defer stop("shutdown")

	for {
		enabled := s.policy().LocalAccessEnabled
		switch {
		case enabled && srv == nil:
			ln, err := platformListen(s.cfg.Path)
			if err != nil {
				s.log.Error("local query API failed to listen; retrying", "path", s.cfg.Path, "err", err)
			} else {
				srv = &http.Server{Handler: s.Handler(), ReadHeaderTimeout: 10 * time.Second}
				go func(hs *http.Server, l net.Listener) {
					if err := hs.Serve(&authListener{Listener: l, log: s.log}); err != nil && !errors.Is(err, http.ErrServerClosed) {
						s.log.Error("local query API serve loop exited", "err", err)
					}
				}(srv, ln)
				loggedDisabled = false
				s.log.Info("local query API listening", "path", s.cfg.Path)
			}
		case !enabled && srv != nil:
			stop("local_access_enabled=false")
		case !enabled && srv == nil && !loggedDisabled:
			loggedDisabled = true
			s.log.Warn("local query API disabled by policy (local_access_enabled=false); not listening", "path", s.cfg.Path)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-gate.C:
		}
	}
}

// handleQuery parses+executes a DSL query against the read-only engine. Sidecars
// are excluded per the contract default; limit is clamped to 1..1000.
func (s *Server) handleQuery(w http.ResponseWriter, r *http.Request) {
	pv := s.policy()
	if !pv.LocalAccessEnabled {
		writeError(w, http.StatusServiceUnavailable, errorBody{Error: "local access disabled by policy", Code: "local_access_disabled"})
		return
	}
	var req QueryRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxRequestBytes))
	dec.DisallowUnknownFields() // mirror the contract's extra="forbid"
	if err := dec.Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, errorBody{Error: "malformed request body", Code: "bad_request", Reason: err.Error()})
		return
	}
	resp, err := runQuery(r.Context(), s.cfg.Searcher, req.Query, req.Limit, req.Offset, pv.Predicates, pv.Stale)
	if err != nil {
		s.writeQueryError(w, err)
		return
	}
	// Record the successful query into the LOCAL-ONLY frecency store (P7-T6). This
	// writes to a physically separate database from the index/outbox, so it can
	// never ride replication — it is architecturally incapable of leaving the
	// machine (internal/history). A recording failure must not fail the query.
	recordHistory(r.Context(), s.cfg.History, req.Query, s.log)
	writeJSON(w, http.StatusOK, resp)
}

// recordHistory best-effort records a successful query's normalized text into the
// local frecency store. rec may be nil (history disabled). Errors are logged, not
// surfaced — a broken history DB must never break the read-only query path.
func recordHistory(ctx context.Context, rec Recorder, raw string, log *slog.Logger) {
	if rec == nil {
		return
	}
	if err := rec.Record(ctx, raw); err != nil && log != nil {
		log.Warn("failed to record local query history (non-fatal)", "err", err)
	}
}

// handleHistory serves GET /v1/history?limit=N — the top frecency-ranked past
// queries on THIS machine, the CLI's `--history` suggestion surface. This is the
// ONLY history READ path, and it is same-user gated by the transport's
// peer-credential check like every other route. Search history never crosses to
// central. When history is disabled (no store) it returns an empty list.
func (s *Server) handleHistory(w http.ResponseWriter, r *http.Request) {
	pv := s.policy()
	if !pv.LocalAccessEnabled {
		writeError(w, http.StatusServiceUnavailable, errorBody{Error: "local access disabled by policy", Code: "local_access_disabled"})
		return
	}
	resp := HistoryResponse{Entries: []HistoryEntry{}}
	if s.cfg.History != nil {
		limit := atoiOr(r.URL.Query().Get("limit"), 20)
		entries, err := s.cfg.History.Top(r.Context(), limit)
		if err != nil {
			if s.log != nil {
				s.log.Error("failed to read local query history", "err", err)
			}
			writeError(w, http.StatusInternalServerError, errorBody{Error: "history read failed", Code: "internal"})
			return
		}
		for _, e := range entries {
			resp.Entries = append(resp.Entries, HistoryEntry{
				Query:    e.Query,
				Hits:     e.Hits,
				LastUsed: e.LastUsed.UTC().Format(time.RFC3339),
				Score:    e.Score,
			})
		}
	}
	writeJSON(w, http.StatusOK, resp)
}

// runQuery is the single query-execution core shared by the socket/pipe transport
// (handleQuery) and the local web UI (P7-T5) so the scope/policy filtering lives
// in exactly one place. It clamps the limit, applies the CACHED path scope
// server-side (NEVER a client-supplied scope, §4.4), slices the offset window, and
// builds the wire QueryResponse. It returns the engine error unmapped so each
// caller can translate it to its own status code via writeQueryError. Sidecars are
// always excluded (contract default).
func runQuery(ctx context.Context, searcher Searcher, raw string, limit, offset int, scope []string, stale bool) (QueryResponse, error) {
	if limit <= 0 {
		limit = defaultLimit
	}
	if limit > maxLimit {
		limit = maxLimit
	}
	if offset < 0 {
		offset = 0
	}

	start := time.Now()
	// Fetch a window covering offset+limit, then slice. The P7-T1 Searcher caps at
	// its limit and returns no separate total, so Total is a floor (== fetched
	// count) and Truncated is set when the fetch hit the cap — a documented
	// approximation; exact pagination totals are deferred.
	fetch := offset + limit
	// Scope comes ONLY from the cached policy, never the client request (§4.4). A
	// stale cache keeps enforcing its last-known predicates (most-restrictive
	// last-known — never widened to unrestricted).
	results, err := searcher.SearchScoped(ctx, raw, false, fetch, scope)
	if err != nil {
		return QueryResponse{}, err
	}
	n := len(results)
	lo := min(offset, n)
	hi := min(offset+limit, n)
	window := results[lo:hi]

	rows := make([]ResultRow, 0, len(window))
	fuzzy := false
	for _, res := range window {
		if res.FuzzyMatched {
			fuzzy = true
		}
		rows = append(rows, toResultRow(res))
	}
	// The fuzzy layer may have engaged for the whole result set even if this window
	// carries no fuzzy row; reflect any fuzzy hit across the full fetch.
	for _, res := range results {
		if res.FuzzyMatched {
			fuzzy = true
			break
		}
	}

	// R3: whenever a path scope narrows results, the response MUST advertise it
	// (Active + the predicate list) so the CLI/web UI can surface a restricted-view
	// note — silent filtering is forbidden. Predicates is always a non-nil list.
	scopePreds := scope
	if scopePreds == nil {
		scopePreds = []string{}
	}
	resp := QueryResponse{
		Rows:      rows,
		Total:     n,
		Truncated: n == fetch,
		Fuzzy:     fuzzy,
		Scope:     ScopeInfo{Active: len(scope) > 0, Predicates: scopePreds, Stale: stale},
		ElapsedMs: time.Since(start).Milliseconds(),
	}
	if fuzzy {
		notice := "results include local typo-tolerant (fuzzy) matches; central search may rank differently"
		resp.Notice = &notice
	}
	return resp, nil
}

// writeQueryError maps a query-engine error to the right status + code (see
// writeEngineError). Kept as a method for the socket handler's call sites.
func (s *Server) writeQueryError(w http.ResponseWriter, err error) {
	writeEngineError(w, s.log, err)
}

// writeEngineError maps a query-engine error to the right status + code — shared
// by the socket transport (handleQuery) and the local web UI (P7-T5). A
// ParseError is a client syntax error (400); an ExecError is a query that parses
// but exceeds the local-index capability boundary (422); anything else is 500.
func writeEngineError(w http.ResponseWriter, log *slog.Logger, err error) {
	var pe *query.ParseError
	var ee *query.ExecError
	switch {
	case errors.As(err, &pe):
		pos := pe.Position
		writeError(w, http.StatusBadRequest, errorBody{
			Error: "query syntax error", Code: pe.Code, Position: &pos, Reason: pe.Reason,
		})
	case errors.As(err, &ee):
		writeError(w, http.StatusUnprocessableEntity, errorBody{
			Error: ee.Message, Code: ee.Code, Keys: ee.Keys,
		})
	default:
		if log != nil {
			log.Error("query execution failed", "err", err)
		}
		writeError(w, http.StatusInternalServerError, errorBody{Error: "query failed", Code: "internal"})
	}
}

// handleHealth reports index readiness + the policy-derived status flags.
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	pv := s.policy()
	resp := HealthResponse{
		Status:       "ok",
		ReadOnly:     true,
		WebUIEnabled: pv.WebUIEnabled,
		AuthRequired: pv.AuthRequired,
		PolicyStale:  pv.Stale,
	}
	if pv.HasVersion {
		v := pv.Version
		resp.PolicyVersion = &v
	}
	if pv.GraceExpiresAt != nil {
		g := pv.GraceExpiresAt.UTC().Format(time.RFC3339)
		resp.OfflineGraceExpiresAt = &g
	}
	if s.cfg.Count != nil {
		if n, err := s.cfg.Count(r.Context()); err != nil {
			resp.Status = "degraded"
			resp.IndexReady = false
		} else {
			resp.ItemCount = n
			resp.IndexReady = true
		}
	} else {
		resp.Status = "starting"
	}
	writeJSON(w, http.StatusOK, resp)
}

// toResultRow projects a query.Result to the R1 narrow wire row.
func toResultRow(res query.Result) ResultRow {
	it := res.Item
	row := ResultRow{
		ID:           it.ID,
		RelPath:      it.RelPath,
		Filename:     it.Filename,
		Extension:    strPtr(it.Extension),
		Size:         it.Size,
		Mtime:        time.Unix(0, it.MtimeNs).UTC().Format(time.RFC3339),
		Kind:         strPtr(it.MediaType),
		QuickHash:    strPtr(it.QuickHash),
		ContentHash:  strPtr(it.ContentHash),
		FuzzyMatched: res.FuzzyMatched,
	}
	if res.FuzzyMatched {
		sc := float64(res.Score)
		row.Score = &sc
	}
	return row
}

// PolicyViewFromRaw derives a PolicyView from a raw central-policy JSON body (the
// PolicyDoc.Policy field), WITHOUT the freshness/scope computation. Absent keys
// take the fail-safe defaults: local access ENABLED (CLI default-on, brief §5.2),
// web UI DISABLED, auth NOT required. Superseded for the daemon gate by
// config.PolicyDoc.LocalSurface (P7-T4, which adds offline-grace + path scope);
// retained for the raw-key smoke test and any freshness-agnostic caller.
func PolicyViewFromRaw(raw []byte, version int, hasVersion bool) PolicyView {
	pv := PolicyView{LocalAccessEnabled: true, Version: version, HasVersion: hasVersion}
	if len(raw) == 0 {
		return pv
	}
	var keys struct {
		LocalAccessEnabled *bool `json:"local_access_enabled"`
		WebUIEnabled       *bool `json:"web_ui_enabled"`
		AuthRequired       *bool `json:"auth_required"`
	}
	if err := json.Unmarshal(raw, &keys); err != nil {
		return pv // a malformed body keeps the fail-safe defaults
	}
	if keys.LocalAccessEnabled != nil {
		pv.LocalAccessEnabled = *keys.LocalAccessEnabled
	}
	if keys.WebUIEnabled != nil {
		pv.WebUIEnabled = *keys.WebUIEnabled
	}
	if keys.AuthRequired != nil {
		pv.AuthRequired = *keys.AuthRequired
	}
	return pv
}

func strPtr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, body errorBody) {
	writeJSON(w, status, body)
}

// authListener wraps the platform listener, dropping any connection whose peer
// process is not the agent's own OS user BEFORE http.Server reads a request.
type authListener struct {
	net.Listener
	log *slog.Logger
}

func (l *authListener) Accept() (net.Conn, error) {
	for {
		c, err := l.Listener.Accept()
		if err != nil {
			return nil, err
		}
		if err := checkPeerConn(c); err != nil {
			l.log.Warn("rejecting local query API connection: peer is not the same OS user", "err", err)
			_ = c.Close()
			continue
		}
		return c, nil
	}
}
