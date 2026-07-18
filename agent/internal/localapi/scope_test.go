package localapi

import (
	"encoding/json"
	"net/http"
	"testing"
)

// A path scope narrows results to the allow-list, advertises itself (R3), and
// never leaks a row outside the scope — the scope comes ONLY from the policy view.
func TestPathScopeNarrowsAndAdvertises(t *testing.T) {
	scoped := func() PolicyView {
		return PolicyView{LocalAccessEnabled: true, Predicates: []string{"Movies/**"}}
	}
	s := testServer(t, scoped)

	// A video lives under Movies/ → in scope → returned.
	resp, raw := doQuery(t, s.Handler(), QueryRequest{Query: "kind:video"})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status=%d body=%s", resp.StatusCode, raw)
	}
	var qr QueryResponse
	if err := json.Unmarshal(raw, &qr); err != nil {
		t.Fatal(err)
	}
	if len(qr.Rows) != 1 || qr.Rows[0].RelPath != "Movies/Arcane.S01E01.mkv" {
		t.Fatalf("in-scope video must return: %+v", qr.Rows)
	}
	// R3: the response advertises the active scope with its predicate list.
	if !qr.Scope.Active || len(qr.Scope.Predicates) != 1 || qr.Scope.Predicates[0] != "Movies/**" {
		t.Fatalf("scope must be advertised active with predicates: %+v", qr.Scope)
	}
}

func TestPathScopeExcludesOutOfScopeRows(t *testing.T) {
	scoped := func() PolicyView {
		return PolicyView{LocalAccessEnabled: true, Predicates: []string{"Movies/**"}}
	}
	s := testServer(t, scoped)

	// Music/Song.flac is OUTSIDE Movies/** → the audio query must return nothing.
	_, raw := doQuery(t, s.Handler(), QueryRequest{Query: "kind:audio"})
	var qr QueryResponse
	json.Unmarshal(raw, &qr)
	if len(qr.Rows) != 0 {
		t.Fatalf("out-of-scope rows must be excluded, got %+v", qr.Rows)
	}
	if !qr.Scope.Active {
		t.Fatalf("scope must still be advertised even with zero rows: %+v", qr.Scope)
	}
}

func TestStaleScopeSurfacedInResponse(t *testing.T) {
	stale := func() PolicyView {
		return PolicyView{LocalAccessEnabled: true, Predicates: []string{"Movies/**"}, Stale: true}
	}
	s := testServer(t, stale)
	_, raw := doQuery(t, s.Handler(), QueryRequest{Query: "kind:video"})
	var qr QueryResponse
	json.Unmarshal(raw, &qr)
	if !qr.Scope.Active || !qr.Scope.Stale {
		t.Fatalf("stale scope must be advertised active+stale: %+v", qr.Scope)
	}
}

// A fuzzy hit outside the scope must not leak (scope applies to the fuzzy pass).
func TestPathScopeAppliesToFuzzyPass(t *testing.T) {
	scoped := func() PolicyView {
		return PolicyView{LocalAccessEnabled: true, Predicates: []string{"Movies/**"}}
	}
	s := testServer(t, scoped)
	// "sogn" (typo of Song, which lives under Music/) triggers the fuzzy re-rank;
	// the scope must still exclude it.
	_, raw := doQuery(t, s.Handler(), QueryRequest{Query: "~sogn"})
	var qr QueryResponse
	json.Unmarshal(raw, &qr)
	for _, r := range qr.Rows {
		if r.RelPath == "Music/Song.flac" {
			t.Fatalf("fuzzy hit outside the scope leaked: %+v", qr.Rows)
		}
	}
}
