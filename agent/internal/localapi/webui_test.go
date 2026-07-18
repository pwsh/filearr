package localapi

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/cookiejar"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/query"
)

// testWebUI wires a WebUIServer over a seeded index at addr, defaulting policy to
// web-UI-enabled + no auth. It reuses seedIndex from server_test.go.
func testWebUI(t *testing.T, addr string, policy func() PolicyView) *WebUIServer {
	t.Helper()
	now := time.Date(2026, 7, 17, 12, 0, 0, 0, time.UTC)
	path := seedIndex(t, now)

	searcher, err := query.NewSearcher(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { searcher.Close() })

	countStore, err := index.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { countStore.Close() })

	ws, err := NewWebUI(WebUIConfig{
		Addr:         addr,
		Searcher:     searcher,
		GateInterval: 15 * time.Millisecond,
		Count: func(ctx context.Context) (int, error) {
			var n int
			err := countStore.DB().QueryRowContext(ctx, `SELECT COUNT(*) FROM items WHERE status='active'`).Scan(&n)
			return n, err
		},
		Policy: policy,
	})
	if err != nil {
		t.Fatal(err)
	}
	return ws
}

// webReq builds a request with an explicit Host (httptest defaults to a NON-loopback
// "example.com", which the Host allow-list would 403).
func webReq(method, target, host string) *http.Request {
	r := httptest.NewRequest(method, target, nil)
	r.Host = host
	return r
}

func enabledPolicy() PolicyView          { return PolicyView{WebUIEnabled: true} }
func enabledPolicyFn() func() PolicyView { return func() PolicyView { return enabledPolicy() } }

// --- Host allow-list (DNS-rebinding defence, §2.2) --------------------------

func TestWebUIHostAllowList(t *testing.T) {
	ws := testWebUI(t, DefaultWebAddr, enabledPolicyFn())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	allowed := []string{"127.0.0.1:8686", "127.0.0.1", "localhost", "localhost:8686", "[::1]:8686", "::1"}
	for _, host := range allowed {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, webReq("GET", "/api/status", host))
		if rec.Code == http.StatusForbidden {
			t.Errorf("loopback host %q was 403'd", host)
		}
	}

	forbidden := []string{"evil.example", "evil.example:8686", "attacker.tld", ""}
	for _, host := range forbidden {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, webReq("GET", "/api/status", host))
		if rec.Code != http.StatusForbidden {
			t.Errorf("non-loopback host %q got %d, want 403", host, rec.Code)
		}
	}
}

// A forged Host must be 403'd BEFORE auth — even carrying a valid session cookie.
func TestWebUIHostCheckBeatsAuth(t *testing.T) {
	ws := testWebUI(t, DefaultWebAddr, func() PolicyView { return PolicyView{WebUIEnabled: true, AuthRequired: true} })
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	req := webReq("GET", "/api/query?q=arcane", "evil.example")
	req.AddCookie(&http.Cookie{Name: webSessionCookie, Value: auth.session})
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("forged Host with a valid cookie must still be 403 (host check pre-handler): got %d", rec.Code)
	}
}

// --- GET/HEAD-only (§3.4) ----------------------------------------------------

func TestWebUIMethodBackstop(t *testing.T) {
	ws := testWebUI(t, DefaultWebAddr, enabledPolicyFn())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	paths := []string{"/", "/api/query", "/api/status", "/app.js"}
	verbs := []string{http.MethodPost, http.MethodPut, http.MethodDelete, http.MethodPatch}
	for _, p := range paths {
		for _, m := range verbs {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, webReq(m, p, "127.0.0.1"))
			if rec.Code != http.StatusMethodNotAllowed {
				t.Errorf("%s %s = %d, want 405", m, p, rec.Code)
			}
		}
	}
	// HEAD on a page is allowed (Go registers HEAD alongside GET).
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("HEAD", "/", "127.0.0.1"))
	if rec.Code == http.StatusMethodNotAllowed {
		t.Errorf("HEAD / must be allowed, got 405")
	}
}

// --- Auth: bootstrap token → cookie exchange (§2.4) --------------------------

func TestWebUIAuthFlow(t *testing.T) {
	ws := testWebUI(t, DefaultWebAddr, func() PolicyView { return PolicyView{WebUIEnabled: true, AuthRequired: true} })
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	// (1) no cookie, no token → API 401.
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/api/status", "127.0.0.1"))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("no-auth API request = %d, want 401", rec.Code)
	}

	// (2) invalid token → 401 (constant-time compare rejects).
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/?token=deadbeefdeadbeef", "127.0.0.1"))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("invalid token = %d, want 401", rec.Code)
	}

	// (3) valid token → 303 redirect, Set-Cookie, token stripped from Location.
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/?token="+auth.token, "127.0.0.1"))
	if rec.Code != http.StatusSeeOther {
		t.Fatalf("valid token exchange = %d, want 303", rec.Code)
	}
	loc := rec.Header().Get("Location")
	if strings.Contains(loc, "token") {
		t.Errorf("redirect Location must strip the token, got %q", loc)
	}
	var sessionVal string
	for _, c := range rec.Result().Cookies() {
		if c.Name == webSessionCookie {
			sessionVal = c.Value
			if !c.HttpOnly || c.SameSite != http.SameSiteStrictMode || c.Path != "/" {
				t.Errorf("session cookie missing hardening flags: %+v", c)
			}
		}
	}
	if sessionVal == "" {
		t.Fatalf("token exchange did not set a session cookie")
	}

	// (4) subsequent request with the cookie → 200.
	req := webReq("GET", "/api/status", "127.0.0.1")
	req.AddCookie(&http.Cookie{Name: webSessionCookie, Value: sessionVal})
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("authenticated request = %d, want 200", rec.Code)
	}

	// (5) a wrong cookie value is rejected.
	req = webReq("GET", "/api/status", "127.0.0.1")
	req.AddCookie(&http.Cookie{Name: webSessionCookie, Value: "not-the-session"})
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("wrong cookie = %d, want 401", rec.Code)
	}
}

// auth_required=false serves without a token (still loopback + Host-checked).
func TestWebUINoAuthServes(t *testing.T) {
	ws := testWebUI(t, DefaultWebAddr, enabledPolicyFn())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/api/query?q=kind:video", "127.0.0.1"))
	if rec.Code != http.StatusOK {
		t.Fatalf("no-auth policy must serve queries: got %d", rec.Code)
	}
	var qr QueryResponse
	json.Unmarshal(rec.Body.Bytes(), &qr)
	if len(qr.Rows) != 1 || qr.Rows[0].RelPath != "Movies/Arcane.S01E01.mkv" {
		t.Fatalf("unexpected rows: %+v", qr.Rows)
	}
}

// --- R3 restricted-view banner data ------------------------------------------

func TestWebUIRestrictedViewData(t *testing.T) {
	scoped := func() PolicyView {
		return PolicyView{WebUIEnabled: true, Predicates: []string{"Movies/*"}, Stale: true}
	}
	ws := testWebUI(t, DefaultWebAddr, scoped)
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	// /api/status carries the banner data so the page renders it before a search.
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/api/status", "127.0.0.1"))
	var st webStatus
	if err := json.Unmarshal(rec.Body.Bytes(), &st); err != nil {
		t.Fatal(err)
	}
	if !st.Scope.Active || len(st.Scope.Predicates) != 1 || st.Scope.Predicates[0] != "Movies/*" {
		t.Errorf("status scope not advertised: %+v", st.Scope)
	}
	if !st.PolicyStale || !st.Scope.Stale {
		t.Errorf("status must reflect stale policy: %+v", st)
	}

	// /api/query also carries scope so the banner updates per search.
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/api/query?q=arcane", "127.0.0.1"))
	var qr QueryResponse
	json.Unmarshal(rec.Body.Bytes(), &qr)
	if !qr.Scope.Active || qr.Scope.Predicates[0] != "Movies/*" || !qr.Scope.Stale {
		t.Errorf("query scope not advertised: %+v", qr.Scope)
	}
	// The scope predicate keeps only Movies/* rows (Docs/Music filtered out).
	for _, row := range qr.Rows {
		if !strings.HasPrefix(row.RelPath, "Movies/") {
			t.Errorf("row outside scope leaked: %s", row.RelPath)
		}
	}
}

// --- 304 conditional request on an embedded asset (§2.1 ETag) ----------------

func TestWebUIAssetETag304(t *testing.T) {
	ws := testWebUI(t, DefaultWebAddr, enabledPolicyFn())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/app.css", "127.0.0.1"))
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /app.css = %d, want 200", rec.Code)
	}
	etag := rec.Header().Get("ETag")
	if etag == "" {
		t.Fatalf("asset served without an ETag (304 impossible)")
	}

	req := webReq("GET", "/app.css", "127.0.0.1")
	req.Header.Set("If-None-Match", etag)
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotModified {
		t.Fatalf("conditional GET with matching ETag = %d, want 304", rec.Code)
	}
}

func TestWebUIIndexServed(t *testing.T) {
	ws := testWebUI(t, DefaultWebAddr, enabledPolicyFn())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/", "127.0.0.1"))
	if rec.Code != http.StatusOK {
		t.Fatalf("GET / = %d, want 200", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/html") {
		t.Errorf("index content-type = %q, want text/html", ct)
	}
	if !strings.Contains(rec.Body.String(), "local search") {
		t.Errorf("index body missing expected content")
	}
}

// --- Query disabled guard ----------------------------------------------------

func TestWebUIQueryRefusedWhenDisabled(t *testing.T) {
	ws := testWebUI(t, DefaultWebAddr, func() PolicyView { return PolicyView{WebUIEnabled: false} })
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/api/query?q=arcane", "127.0.0.1"))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("disabled web UI must refuse queries: got %d", rec.Code)
	}
}

// --- Loopback bind-address validation (§2.1 0.0.0.0-day) ---------------------

func TestValidateLoopbackAddr(t *testing.T) {
	ok := []string{"127.0.0.1:8686", "localhost:8686", "[::1]:8686", "127.0.0.5:1234"}
	for _, a := range ok {
		if err := validateLoopbackAddr(a); err != nil {
			t.Errorf("loopback addr %q rejected: %v", a, err)
		}
	}
	bad := []string{"0.0.0.0:8686", ":8686", "192.168.1.10:8686", "example.com:8686", "127.0.0.1"}
	for _, a := range bad {
		if err := validateLoopbackAddr(a); err == nil {
			t.Errorf("non-loopback/invalid addr %q accepted", a)
		}
	}
}

// --- Live lifecycle gating: policy flip takes the listener up/down -----------

func TestWebUILifecycleGating(t *testing.T) {
	// Grab a free loopback port, then hand it to the server on a fixed addr so we
	// can probe the same port across up/down transitions.
	probe, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := probe.Addr().String()
	probe.Close()

	var enabled atomic.Bool
	enabled.Store(true)
	ws := testWebUI(t, addr, func() PolicyView { return PolicyView{WebUIEnabled: enabled.Load()} })

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go ws.Run(ctx)

	waitListen(t, addr, true, "web UI should come up when enabled")
	enabled.Store(false)
	waitListen(t, addr, false, "web UI should shut down within one gate tick when disabled")
	enabled.Store(true)
	waitListen(t, addr, true, "web UI should come back up when re-enabled")
}

// TestWebUILiveAuthRoundTrip drives the REAL http.Server (not a recorder) through
// the full bootstrap-token → cookie → query flow with a cookie-jar client that
// follows the strip-token redirect, over an actual loopback socket.
func TestWebUILiveAuthRoundTrip(t *testing.T) {
	probe, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := probe.Addr().String()
	probe.Close()

	ws := testWebUI(t, addr, func() PolicyView { return PolicyView{WebUIEnabled: true, AuthRequired: true} })
	// Fix the credential so the test knows the token (Run generates it internally,
	// so instead build our own handler+server on the same addr).
	auth, _ := newWebAuth()
	srv := &http.Server{Handler: ws.buildHandler(auth), ReadHeaderTimeout: 5 * time.Second}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Serve(ln)
	t.Cleanup(func() { srv.Close() })

	jar, _ := cookiejar.New(nil)
	client := &http.Client{Jar: jar, Timeout: 3 * time.Second}
	base := "http://" + addr

	// Unauthenticated API call → 401.
	resp, err := client.Get(base + "/api/status")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("pre-auth /api/status = %d, want 401", resp.StatusCode)
	}

	// Hit the tokenized URL: the client follows the 303 to "/" and stores the
	// session cookie; the final page is 200 HTML.
	resp, err = client.Get(base + "/?token=" + auth.token)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("token URL (after redirect) = %d, want 200", resp.StatusCode)
	}
	if resp.Request.URL.RawQuery != "" {
		t.Errorf("token not stripped from final URL: %q", resp.Request.URL.String())
	}

	// The jar now carries the session cookie → the query API answers.
	resp, err = client.Get(base + "/api/query?q=kind:video")
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("authenticated /api/query = %d, want 200 (body=%s)", resp.StatusCode, body)
	}
	var qr QueryResponse
	if err := json.Unmarshal(body, &qr); err != nil {
		t.Fatal(err)
	}
	if len(qr.Rows) != 1 || qr.Rows[0].RelPath != "Movies/Arcane.S01E01.mkv" {
		t.Fatalf("unexpected rows over live transport: %+v", qr.Rows)
	}
}

// waitListen polls addr until it is (or is not) accepting connections, or fails.
func waitListen(t *testing.T, addr string, wantUp bool, msg string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		c, err := net.DialTimeout("tcp", addr, 100*time.Millisecond)
		up := err == nil
		if up {
			c.Close()
		}
		if up == wantUp {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("%s (addr=%s wantUp=%v)", msg, addr, wantUp)
}
