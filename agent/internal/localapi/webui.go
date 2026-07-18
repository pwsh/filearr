package localapi

// webui.go is the P7-T5 local web UI: a minimal, read-only browser search surface
// served over loopback TCP (browsers cannot dial the P7-T2 unix socket / named
// pipe). It is a SEPARATE listener from the socket/pipe query transport and shares
// only the read-only query engine + the scope/policy logic (runQuery in server.go).
//
// Security posture (research §2, CLAUDE.md priority order security > …):
//   - Bound to 127.0.0.1 ONLY, never 0.0.0.0 (the "0.0.0.0-day" class, §2.1). The
//     bind address is validated to be a loopback literal; a non-loopback addr is
//     refused (fail closed).
//   - Host-header allow-list rejects anything but localhost / 127.0.0.1 / [::1]
//     PRE-handler with 403 — the DNS-rebinding defence (Syncthing pattern, §2.2).
//     No skip-check escape hatch.
//   - GET/HEAD-only: method-scoped routes register no mutating handler, and a
//     global backstop 405s any other verb (§3.4 layered read-only enforcement).
//   - CSRF: net/http.CrossOriginProtection (stdlib) wraps the mux (§2.3).
//   - Auth (when policy auth_required): a random per-listen bootstrap token is
//     printed to the daemon log as a http://127.0.0.1:PORT/?token=… URL; a valid
//     token is exchanged (GET → Set-Cookie → redirect stripping the token) for an
//     HttpOnly + SameSite=Strict + Path-scoped session cookie (Jupyter's pattern,
//     §2.4). Constant-time token/cookie compare.
//   - Policy-gated: the listener serves ONLY while the EFFECTIVE web-UI capability
//     is on (web_ui_enabled AND policy fresh within grace, PolicyView.WebUIEnabled
//     computed by config.LocalSurface). Central disabling it, or the policy going
//     stale past grace, takes the UI down within one gate tick — the query socket
//     transport is unaffected (R4 asymmetry). A never-contacted agent: web UI off.

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"embed"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"mime"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

//go:embed assets
var assetsFS embed.FS

// DefaultWebAddr is the default loopback bind address for the local web UI. 8686
// is a stable, documented default (flag -web-addr / env FILEARR_AGENT_WEBUI_ADDR
// override). It is loopback-only; a non-loopback override is refused.
const DefaultWebAddr = "127.0.0.1:8686"

// webSessionCookie is the HttpOnly session cookie name set after a successful
// bootstrap-token exchange.
const webSessionCookie = "filearr_session"

// webGateInterval is how often Run re-reads the policy gate to honor a
// web_ui_enabled flip / stale transition. Kept below the poll floor so a disable
// (or a stale-past-grace transition) is honored within one poll interval (R4).
const webGateInterval = 30 * time.Second

func init() {
	// Windows registry MIME lookups are unreliable (golang/go#32350 — .js can be
	// served as text/plain), so pin the extensions the embedded assets use.
	_ = mime.AddExtensionType(".js", "text/javascript; charset=utf-8")
	_ = mime.AddExtensionType(".css", "text/css; charset=utf-8")
	_ = mime.AddExtensionType(".html", "text/html; charset=utf-8")
}

// WebUIConfig wires a WebUIServer.
type WebUIConfig struct {
	// Addr is the loopback bind address (host:port). Must be a loopback literal.
	Addr string
	// Searcher is the read-only query engine (shared shape with the socket server).
	Searcher Searcher
	// Count reports the local index item count for the status probe.
	Count func(ctx context.Context) (int, error)
	// Recorder records a successful query into the LOCAL-ONLY frecency store
	// (P7-T6). The web UI is given ONLY the write-side interface — it structurally
	// cannot read history back (read access is reserved to the socket API). nil
	// disables recording. History never leaves the machine (internal/history).
	Recorder Recorder
	// Policy returns a live snapshot of the cached-policy gate (WebUIEnabled is the
	// EFFECTIVE capability: intent AND fresh). nil defaults to all-off (fail closed).
	Policy func() PolicyView
	// GateInterval overrides the policy re-check cadence (test seam).
	GateInterval time.Duration
	Logger       *slog.Logger
	// Now is a clock seam (unused for the web UI today; reserved for parity).
	Now func() time.Time
}

// WebUIServer serves the read-only local web UI.
type WebUIServer struct {
	cfg          WebUIConfig
	log          *slog.Logger
	policy       func() PolicyView
	gateInterval time.Duration
	assets       fs.FS             // the "assets" subtree of assetsFS
	etags        map[string]string // asset path (no leading slash) → strong ETag
}

// NewWebUI constructs a WebUIServer, applying defaults and precomputing the
// content-hash ETags for the embedded assets (embed.FS files have a zero ModTime,
// so a content-hash ETag is what makes conditional requests 304 — §2.1).
func NewWebUI(cfg WebUIConfig) (*WebUIServer, error) {
	sub, err := fs.Sub(assetsFS, "assets")
	if err != nil {
		return nil, fmt.Errorf("sub assets fs: %w", err)
	}
	ws := &WebUIServer{
		cfg:          cfg,
		log:          cfg.Logger,
		policy:       cfg.Policy,
		gateInterval: cfg.GateInterval,
		assets:       sub,
		etags:        map[string]string{},
	}
	if ws.log == nil {
		ws.log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if ws.policy == nil {
		ws.policy = func() PolicyView { return PolicyView{} } // fail closed
	}
	if ws.gateInterval <= 0 {
		ws.gateInterval = webGateInterval
	}
	if cfg.Addr == "" {
		ws.cfg.Addr = DefaultWebAddr
	}
	if err := computeETags(sub, ws.etags); err != nil {
		return nil, err
	}
	return ws, nil
}

// computeETags walks the embedded asset tree and records a strong content-hash
// ETag per file so ServeContent can answer If-None-Match with 304.
func computeETags(fsys fs.FS, out map[string]string) error {
	return fs.WalkDir(fsys, ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		b, rerr := fs.ReadFile(fsys, p)
		if rerr != nil {
			return rerr
		}
		sum := sha256.Sum256(b)
		out[p] = `"` + hex.EncodeToString(sum[:16]) + `"`
		return nil
	})
}

// webAuth is the per-listen credential pair: the bootstrap token (printed in the
// URL) and the session-cookie secret it is exchanged for. Both are fresh random
// values per listener start, so a UI bounce rotates them.
type webAuth struct {
	token   string
	session string
}

func newWebAuth() (webAuth, error) {
	tok, err := randHex(32)
	if err != nil {
		return webAuth{}, err
	}
	sess, err := randHex(32)
	if err != nil {
		return webAuth{}, err
	}
	return webAuth{token: tok, session: sess}, nil
}

func randHex(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// Run owns the web-UI listener lifecycle. It listens+serves while the cached
// policy's EFFECTIVE web-UI capability is on; it stops within one gate interval
// when that flips off (central disable OR policy stale past grace — R4). Returns
// ctx.Err() on shutdown.
func (ws *WebUIServer) Run(ctx context.Context) error {
	if err := validateLoopbackAddr(ws.cfg.Addr); err != nil {
		ws.log.Error("local web UI disabled: invalid (non-loopback) bind address; refusing", "addr", ws.cfg.Addr, "err", err)
		// Wait for shutdown rather than busy-spinning on an unfixable config.
		<-ctx.Done()
		return ctx.Err()
	}

	gate := time.NewTicker(ws.gateInterval)
	defer gate.Stop()

	var srv *http.Server
	loggedDisabled := false
	stop := func(reason string) {
		if srv != nil {
			ws.log.Info("stopping local web UI", "reason", reason, "addr", ws.cfg.Addr)
			_ = srv.Close()
			srv = nil
		}
	}
	defer stop("shutdown")

	for {
		enabled := ws.policy().WebUIEnabled
		switch {
		case enabled && srv == nil:
			ln, err := net.Listen("tcp", ws.cfg.Addr)
			if err != nil {
				ws.log.Error("local web UI failed to listen; retrying", "addr", ws.cfg.Addr, "err", err)
			} else {
				auth, aerr := newWebAuth()
				if aerr != nil {
					ln.Close()
					ws.log.Error("local web UI failed to generate credentials; retrying", "err", aerr)
				} else {
					srv = &http.Server{
						Handler:           ws.buildHandler(auth),
						ReadHeaderTimeout: 10 * time.Second,
						IdleTimeout:       120 * time.Second,
					}
					go func(hs *http.Server, l net.Listener) {
						if err := hs.Serve(l); err != nil && !errors.Is(err, http.ErrServerClosed) {
							ws.log.Error("local web UI serve loop exited", "err", err)
						}
					}(srv, ln)
					loggedDisabled = false
					ws.announce(ln.Addr().String(), auth)
				}
			}
		case !enabled && srv != nil:
			stop("web_ui_enabled=false or policy stale past grace")
		case !enabled && srv == nil && !loggedDisabled:
			loggedDisabled = true
			ws.log.Info("local web UI disabled by policy (web_ui_enabled off or never enabled); not listening")
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-gate.C:
		}
	}
}

// announce prints the access URL (with the bootstrap token when auth is required)
// to the daemon log AND stdout — the operator copies it into a browser (§2.4).
func (ws *WebUIServer) announce(addr string, auth webAuth) {
	base := "http://" + addr + "/"
	pv := ws.policy()
	if pv.AuthRequired {
		full := base + "?token=" + auth.token
		ws.log.Info("local web UI listening (auth required) — open the tokenized URL below", "url", full)
		fmt.Printf("filearr local web UI: open %s\n", full)
	} else {
		ws.log.Info("local web UI listening (no auth required)", "url", base)
		fmt.Printf("filearr local web UI: open %s\n", base)
	}
}

// buildHandler assembles the middleware chain (outermost first):
//
//	hostAllowList → methodBackstop → CrossOriginProtection → authGate → mux
//
// Host allow-list runs FIRST so a forged Host is 403'd before any handler (incl.
// before auth), the DNS-rebinding defence. The method backstop 405s any non-GET/
// HEAD verb. CrossOriginProtection is stdlib CSRF. authGate enforces the session
// cookie (and performs the token exchange) when policy auth_required.
func (ws *WebUIServer) buildHandler(auth webAuth) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("GET /api/query", http.HandlerFunc(ws.handleQuery))
	mux.Handle("GET /api/status", http.HandlerFunc(ws.handleStatus))
	mux.Handle("GET /", ws.staticHandler())

	cop := http.NewCrossOriginProtection()
	chain := ws.authGate(auth, mux)
	chain = cop.Handler(chain)
	chain = methodBackstop(chain)
	chain = hostAllowList(chain)
	return chain
}

// hostAllowList rejects any request whose Host is not a loopback name/literal with
// 403 BEFORE any handler runs — the Syncthing-style DNS-rebinding defence (§2.2).
// There is deliberately no skip-check escape hatch.
func hostAllowList(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !hostAllowed(r.Host) {
			writeError(w, http.StatusForbidden, errorBody{
				Error: "forbidden host header (local web UI accepts localhost only)",
				Code:  "forbidden_host",
			})
			return
		}
		next.ServeHTTP(w, r)
	})
}

// hostAllowed reports whether the Host header names loopback: localhost,
// 127.0.0.0/8, or ::1 (with or without a :port). A hostile domain resolving to
// 127.0.0.1 sends its own name as Host and is rejected here.
func hostAllowed(host string) bool {
	if host == "" {
		return false
	}
	h := host
	if hostname, _, err := net.SplitHostPort(host); err == nil {
		h = hostname
	}
	// Strip any leftover IPv6 brackets (e.g. a bare "[::1]" with no port).
	h = strings.TrimSuffix(strings.TrimPrefix(h, "["), "]")
	switch strings.ToLower(h) {
	case "localhost", "127.0.0.1", "::1":
		return true
	}
	if ip := net.ParseIP(h); ip != nil && ip.IsLoopback() {
		return true
	}
	return false
}

// validateLoopbackAddr refuses any bind address that is not an explicit loopback
// literal (the 0.0.0.0-day defence, §2.1). It requires a host:port form.
func validateLoopbackAddr(addr string) error {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("bind address must be host:port: %w", err)
	}
	if host == "" {
		return fmt.Errorf("empty bind host is treated as 0.0.0.0; refusing")
	}
	if strings.EqualFold(host, "localhost") {
		return nil
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return fmt.Errorf("bind host %q is not an IP literal or localhost", host)
	}
	if !ip.IsLoopback() {
		return fmt.Errorf("bind host %q is not loopback; the web UI is loopback-only", host)
	}
	return nil
}

// authGate enforces the session cookie when policy auth_required. With no valid
// cookie it performs the one-time bootstrap-token exchange: a request carrying a
// valid ?token= gets the HttpOnly + SameSite=Strict session cookie and a redirect
// that strips the token from the URL (Jupyter's pattern). Otherwise it 401s. When
// auth is not required the gate is a pass-through (still loopback + Host-checked).
func (ws *WebUIServer) authGate(auth webAuth, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !ws.policy().AuthRequired {
			next.ServeHTTP(w, r)
			return
		}
		if hasValidSession(r, auth.session) {
			next.ServeHTTP(w, r)
			return
		}
		if tok := r.URL.Query().Get("token"); tok != "" && constantTimeEq(tok, auth.token) {
			http.SetCookie(w, &http.Cookie{
				Name:     webSessionCookie,
				Value:    auth.session,
				Path:     "/",
				HttpOnly: true,
				SameSite: http.SameSiteStrictMode,
				Secure:   false, // loopback plaintext http
			})
			// Redirect to the same path with the token stripped (never leave the
			// token in history / logs / Referer).
			stripped := stripToken(r.URL)
			http.Redirect(w, r, stripped, http.StatusSeeOther)
			return
		}
		// No cookie, no valid token. For API routes answer JSON 401; for pages a
		// tiny HTML note (avoids a blank browser page).
		if strings.HasPrefix(r.URL.Path, "/api/") {
			writeError(w, http.StatusUnauthorized, errorBody{
				Error: "unauthorized: a valid bootstrap token or session cookie is required",
				Code:  "unauthorized",
			})
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(w, unauthorizedPage)
	})
}

const unauthorizedPage = `<!doctype html><meta charset="utf-8"><title>Filearr — token required</title>` +
	`<body style="font:15px system-ui;max-width:38rem;margin:3rem auto;padding:0 1rem">` +
	`<h1>Token required</h1><p>Open the tokenized <code>http://127.0.0.1:PORT/?token=…</code> ` +
	`URL printed in the agent log to sign in to the local web UI.</p></body>`

// hasValidSession constant-time compares the session cookie to the expected value.
func hasValidSession(r *http.Request, want string) bool {
	c, err := r.Cookie(webSessionCookie)
	if err != nil {
		return false
	}
	return constantTimeEq(c.Value, want)
}

func constantTimeEq(a, b string) bool {
	return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1
}

// stripToken returns the request path with the token query param removed (all
// other params preserved).
func stripToken(u *url.URL) string {
	q := u.Query()
	q.Del("token")
	out := u.Path
	if enc := q.Encode(); enc != "" {
		out += "?" + enc
	}
	return out
}

// staticHandler serves the embedded assets via http.FileServerFS, injecting the
// precomputed content-hash ETag so ServeContent answers If-None-Match with 304
// (embed.FS files carry a zero ModTime, §2.1). It never serves a directory listing
// beyond the FileServer's index.html mapping.
func (ws *WebUIServer) staticHandler() http.Handler {
	fileServer := http.FileServerFS(ws.assets)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := strings.TrimPrefix(r.URL.Path, "/")
		if key == "" {
			key = "index.html"
		}
		if etag, ok := ws.etags[key]; ok {
			w.Header().Set("ETag", etag)
			w.Header().Set("Cache-Control", "no-cache")
		}
		// A conservative, page-appropriate CSP: everything from self, no framing.
		w.Header().Set("Content-Security-Policy",
			"default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		fileServer.ServeHTTP(w, r)
	})
}

// handleQuery serves the web UI's JSON query endpoint (GET /api/query?q=&limit=&
// offset=). It runs through the SAME runQuery core as the socket transport, so the
// cached path scope is applied server-side (never from the client) identically.
func (ws *WebUIServer) handleQuery(w http.ResponseWriter, r *http.Request) {
	pv := ws.policy()
	if !pv.WebUIEnabled {
		writeError(w, http.StatusServiceUnavailable, errorBody{Error: "web UI disabled by policy", Code: "web_ui_disabled"})
		return
	}
	q := r.URL.Query()
	resp, err := runQuery(r.Context(), ws.cfg.Searcher, q.Get("q"), atoiOr(q.Get("limit"), 0), atoiOr(q.Get("offset"), 0), pv.Predicates, pv.Stale)
	if err != nil {
		writeEngineError(w, ws.log, err)
		return
	}
	// Record the successful query into the LOCAL-ONLY frecency store (P7-T6), same
	// as the socket path. Best-effort; a recording failure never fails the query.
	recordHistory(r.Context(), ws.cfg.Recorder, q.Get("q"), ws.log)
	writeJSON(w, http.StatusOK, resp)
}

// webStatus is the small status payload the page loads on startup so the
// restricted-view / stale banners render before the first search (R3).
type webStatus struct {
	IndexReady  bool      `json:"index_ready"`
	ItemCount   int       `json:"item_count"`
	ReadOnly    bool      `json:"read_only"`
	PolicyStale bool      `json:"policy_stale"`
	Scope       ScopeInfo `json:"scope"`
}

func (ws *WebUIServer) handleStatus(w http.ResponseWriter, r *http.Request) {
	pv := ws.policy()
	preds := pv.Predicates
	if preds == nil {
		preds = []string{}
	}
	st := webStatus{
		ReadOnly:    true,
		PolicyStale: pv.Stale,
		Scope:       ScopeInfo{Active: len(pv.Predicates) > 0, Predicates: preds, Stale: pv.Stale},
	}
	if ws.cfg.Count != nil {
		if n, err := ws.cfg.Count(r.Context()); err == nil {
			st.ItemCount = n
			st.IndexReady = true
		}
	}
	writeJSON(w, http.StatusOK, st)
}

func atoiOr(s string, def int) int {
	if s == "" {
		return def
	}
	if n, err := strconv.Atoi(s); err == nil {
		return n
	}
	return def
}
