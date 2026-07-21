package config

import (
	"context"
	"sync"
	"testing"

	"net/http/httptest"
)

func TestPolicyTaxonomyVersionValue(t *testing.T) {
	if got := (Policy{}).TaxonomyVersionValue(); got != 0 {
		t.Errorf("absent taxonomy_version => %d, want 0", got)
	}
	pol, err := ParsePolicy([]byte(`{"taxonomy_version": 7}`))
	if err != nil {
		t.Fatal(err)
	}
	if got := pol.TaxonomyVersionValue(); got != 7 {
		t.Errorf("taxonomy_version => %d, want 7", got)
	}
}

// TestPollerAfterFetchFires verifies the W8-E post-fetch hook runs on a 200
// (fresh body) AND on a 304 (cache current) — both confirm central contact, and
// a taxonomy edit that only bumps the ETag must still surface the new version to
// the hook even when the apply seam does not fire.
func TestPollerAfterFetchFires(t *testing.T) {
	mc := newMockCentral("global", 1, `{"taxonomy_version": 4}`)
	srv := httptest.NewServer(mc.handler())
	defer srv.Close()

	var mu sync.Mutex
	var seen []int
	p := NewPoller(PollerConfig{
		Client: NewPolicyClient(ClientConfig{BaseURL: srv.URL, AgentID: "aid"}),
		Cache:  NewETagCache(t.TempDir()),
		AfterFetch: func(_ context.Context, doc PolicyDoc) {
			pol, _ := doc.Parsed()
			mu.Lock()
			seen = append(seen, pol.TaxonomyVersionValue())
			mu.Unlock()
		},
	})

	// First poll: 200 (applied). Second poll: 304 (cache current). Both fire.
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeApplied {
		t.Fatalf("first poll: outcome=%v err=%v", o, err)
	}
	if _, o, err := p.PollOnce(context.Background()); err != nil || o != OutcomeNotModified {
		t.Fatalf("second poll: outcome=%v err=%v", o, err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 2 {
		t.Fatalf("AfterFetch fired %d times, want 2 (200 + 304)", len(seen))
	}
	for i, v := range seen {
		if v != 4 {
			t.Errorf("AfterFetch[%d] taxonomy_version = %d, want 4", i, v)
		}
	}
}
