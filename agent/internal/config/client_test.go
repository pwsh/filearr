package config

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

// mockCentral is an in-memory port of the central policy endpoint honoring the
// frozen contract: ETag "<scope>/<version>", 304 on a matching If-None-Match
// (ETag still present), and it records the ?applied= param each request carried.
type mockCentral struct {
	mu sync.Mutex

	scope   string
	version int
	body    json.RawMessage // the "policy" sub-object

	appliedSeen []int // ?applied= per request, in order
	requests    int

	status int // if non-zero, always return this status (error simulation)
}

func newMockCentral(scope string, version int, body string) *mockCentral {
	return &mockCentral{scope: scope, version: version, body: json.RawMessage(body)}
}

func (m *mockCentral) etag() string {
	return fmt.Sprintf(`"%s/%d"`, m.scope, m.version)
}

// set atomically publishes a new policy version.
func (m *mockCentral) set(scope string, version int, body string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.scope, m.version, m.body = scope, version, json.RawMessage(body)
}

func (m *mockCentral) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		m.requests++
		var applied int
		fmt.Sscanf(r.URL.Query().Get("applied"), "%d", &applied)
		m.appliedSeen = append(m.appliedSeen, applied)

		if m.status != 0 {
			http.Error(w, `{"detail":"boom"}`, m.status)
			return
		}
		etag := m.etag()
		w.Header().Set("ETag", etag) // present on BOTH 200 and 304 per contract
		if inm := r.Header.Get("If-None-Match"); inm == etag {
			w.WriteHeader(http.StatusNotModified)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"scope":   m.scope,
			"version": m.version,
			"policy":  m.body,
		})
	})
}

func (m *mockCentral) applied() []int {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]int, len(m.appliedSeen))
	copy(out, m.appliedSeen)
	return out
}

func TestClientETagRoundTrip(t *testing.T) {
	mc := newMockCentral("library", 7, `{"presets":["system_files"]}`)
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	c := NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "agent-1"})

	// First fetch (no cached ETag): full 200.
	res, err := c.Fetch(context.Background(), "", 0)
	if err != nil {
		t.Fatalf("first fetch: %v", err)
	}
	if res.Version != 7 || res.Scope != "library" {
		t.Errorf("unexpected result: %+v", res)
	}
	if res.ETag != `"library/7"` {
		t.Errorf("ETag = %q", res.ETag)
	}
	if !json.Valid(res.Policy) || string(res.Policy) != `{"presets":["system_files"]}` {
		t.Errorf("policy body = %s", res.Policy)
	}

	// Second fetch WITH the ETag: 304.
	_, err = c.Fetch(context.Background(), res.ETag, res.Version)
	if err != ErrNotModified {
		t.Fatalf("expected ErrNotModified, got %v", err)
	}

	// After central bumps the version, the stale ETag no longer matches → 200.
	mc.set("library", 8, `{"presets":[]}`)
	res2, err := c.Fetch(context.Background(), res.ETag, res.Version)
	if err != nil {
		t.Fatalf("post-bump fetch: %v", err)
	}
	if res2.Version != 8 || res2.ETag != `"library/8"` {
		t.Errorf("post-bump result: %+v", res2)
	}

	// The ?applied= param carried the version we reported each time.
	if got := mc.applied(); len(got) != 3 || got[0] != 0 || got[1] != 7 || got[2] != 7 {
		t.Errorf("applied params = %v, want [0 7 7]", got)
	}
}

func TestClientNoPolicyState(t *testing.T) {
	mc := newMockCentral("none", 0, `{}`)
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	c := NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a"})
	res, err := c.Fetch(context.Background(), "", 0)
	if err != nil {
		t.Fatal(err)
	}
	if res.Scope != "none" || res.Version != 0 {
		t.Errorf("no-policy state: %+v", res)
	}
	pol, _ := ParsePolicy(res.Policy)
	if pol.Presets != nil {
		t.Error("no-policy body must yield all-absent policy")
	}
}

func TestClientAuthHeader(t *testing.T) {
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		w.Header().Set("ETag", `"none/0"`)
		_ = json.NewEncoder(w).Encode(map[string]any{"scope": "none", "version": 0, "policy": map[string]any{}})
	}))
	defer srv.Close()

	c := NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a", AuthFn: func() string { return "fp-token" }})
	if _, err := c.Fetch(context.Background(), "", 0); err != nil {
		t.Fatal(err)
	}
	if gotAuth != "Bearer fp-token" {
		t.Errorf("Authorization = %q", gotAuth)
	}
}

func TestClientErrorStatuses(t *testing.T) {
	mc := newMockCentral("library", 1, `{}`)
	mc.status = http.StatusNotFound
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	c := NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "a"})
	_, err := c.Fetch(context.Background(), "", 0)
	if err == nil || err == ErrNotModified {
		t.Fatalf("expected a hard error for 404, got %v", err)
	}
}
