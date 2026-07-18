package query

import (
	"context"
	"database/sql"
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/filearr/filearr/agent/internal/index"
)

// Bounds for the R5 fuzzy re-rank layer (kept simple + bounded).
const (
	// fuzzyMaxDistance is the Levenshtein ceiling for a token to count as a
	// fuzzy match (a single typo/transposition ≈ 1-2 edits).
	fuzzyMaxDistance = 2
	// fuzzyCandidateCap bounds the in-process re-rank pool so a huge index never
	// turns a fuzzy query into an unbounded scan.
	fuzzyCandidateCap = 5000
	// defaultLimit caps result rows when the caller passes limit <= 0.
	defaultLimit = 50
)

// Result is one search hit: the item plus fuzzy-layer annotations. FuzzyMatched
// is true when the row came from the R5 re-rank pass (not an exact/substring
// hit); Score is the summed edit distance of the approximate terms (0 for exact
// results), lower is better.
type Result struct {
	Item         index.Item
	FuzzyMatched bool
	Score        int
}

// Searcher owns a dedicated read-only connection to the local index — strictly
// separate from the writable Store (P7-T1 defense in depth: no DSL input can
// coerce a write on this handle). Construct with NewSearcher; call Close when done.
type Searcher struct {
	db  *sql.DB
	now func() time.Time
}

// NewSearcher opens a read-only Searcher over the index at path.
func NewSearcher(path string) (*Searcher, error) {
	db, err := index.OpenReadOnly(path)
	if err != nil {
		return nil, err
	}
	return &Searcher{db: db, now: time.Now}, nil
}

// Close releases the read-only connection.
func (s *Searcher) Close() error { return s.db.Close() }

// Search parses, compiles, and executes a DSL query string against the local
// index, applying the R5 fuzzy heuristic (re-rank when the exact query yields
// zero hits OR any ~ term is present). includeSidecars=false hides is_sidecar
// rows (default parity). A *ParseError is returned for malformed input; an
// *ExecError for a query that parses but exceeds the local-index capability
// boundary (tag/meta./cf.) — both are distinguishable via errors.As.
func (s *Searcher) Search(ctx context.Context, raw string, includeSidecars bool, limit int) ([]Result, error) {
	return s.SearchScoped(ctx, raw, includeSidecars, limit, nil)
}

// SearchScoped is Search with a server-side path-scope allow-list (P7-T4). scope
// is the flattened rel_path GLOB predicate list from the agent's CACHED policy —
// NEVER a client-supplied value (research §4.4). When non-empty, a row must match
// at least one predicate (OR-combined allow-list); an empty scope is unrestricted.
// The predicates are applied identically to the exact and fuzzy passes, so a
// fuzzy hit outside the scope can never leak.
func (s *Searcher) SearchScoped(ctx context.Context, raw string, includeSidecars bool, limit int, scope []string) ([]Result, error) {
	q, pe := Parse(raw)
	if pe != nil {
		return nil, pe
	}
	return s.searchAST(ctx, q, includeSidecars, limit, scope)
}

// SearchAST executes an already-parsed Query (used by callers that parse once).
func (s *Searcher) SearchAST(ctx context.Context, q Query, includeSidecars bool, limit int) ([]Result, error) {
	return s.searchAST(ctx, q, includeSidecars, limit, nil)
}

func (s *Searcher) searchAST(ctx context.Context, q Query, includeSidecars bool, limit int, scope []string) ([]Result, error) {
	if limit <= 0 {
		limit = defaultLimit
	}
	c, ee := compile(q, s.now())
	if ee != nil {
		return nil, ee
	}
	applyScope(c, scope)

	// --- Exact pass -------------------------------------------------------
	exact, err := s.runExact(ctx, c, includeSidecars, limit)
	if err != nil {
		return nil, err
	}

	// R5 heuristic: fuzzy re-rank fires ONLY when there were zero exact/substring
	// hits OR the query carried an explicit ~ term. Otherwise return exact hits.
	fuzzyTrigger := len(c.fzTerms) > 0 || len(exact) == 0
	if !fuzzyTrigger {
		return exact, nil
	}

	// Choose approximate vs exact-substring targets for the fuzzy pass.
	var approx, mustContain []string
	if len(c.fzTerms) > 0 {
		approx = c.fzTerms       // explicit ~ terms are matched approximately
		mustContain = c.posTerms // any non-~ positive terms stay exact substrings
	} else {
		approx = c.posTerms // zero-hit fallback: all positive terms go fuzzy
	}
	if len(approx) == 0 {
		// Nothing to approximate (e.g. a pure-filter query that matched nothing):
		// there is no typo to tolerate, so the exact (possibly empty) result stands.
		return exact, nil
	}

	return s.runFuzzy(ctx, c, includeSidecars, limit, approx, mustContain)
}

// runExact executes the compiled exact query (FTS MATCH + filter WHERE).
func (s *Searcher) runExact(ctx context.Context, c *compiled, includeSidecars bool, limit int) ([]Result, error) {
	var sb strings.Builder
	sb.WriteString(index.SelectItemColumnsQualified)
	args := make([]any, 0, len(c.args)+2)
	if c.ftsMatch != "" {
		sb.WriteString("\nJOIN items_fts ON items_fts.rowid = items.rowid")
	}
	where := append([]string{}, c.where...)
	if c.ftsMatch != "" {
		where = append([]string{"items_fts MATCH ?"}, where...)
		args = append(args, c.ftsMatch)
	}
	args = append(args, c.args...)
	if !includeSidecars {
		where = append(where, "items.is_sidecar = 0")
	}
	if len(where) > 0 {
		sb.WriteString("\nWHERE ")
		sb.WriteString(strings.Join(where, " AND "))
	}
	if c.ftsMatch != "" {
		sb.WriteString("\nORDER BY rank")
	} else {
		sb.WriteString("\nORDER BY items.rel_path")
	}
	sb.WriteString("\nLIMIT ?")
	args = append(args, limit)

	items, err := s.queryItems(ctx, sb.String(), args)
	if err != nil {
		return nil, err
	}
	out := make([]Result, len(items))
	for i := range items {
		out[i] = Result{Item: items[i]}
	}
	return out, nil
}

// runFuzzy builds a bounded candidate pool (all filters + negated terms, but NOT
// the positive FTS match) and re-ranks it in process by edit distance.
func (s *Searcher) runFuzzy(ctx context.Context, c *compiled, includeSidecars bool, limit int, approx, mustContain []string) ([]Result, error) {
	var sb strings.Builder
	sb.WriteString(index.SelectItemColumnsQualified)
	where := append([]string{}, c.where...)
	if !includeSidecars {
		where = append(where, "items.is_sidecar = 0")
	}
	if len(where) > 0 {
		sb.WriteString("\nWHERE ")
		sb.WriteString(strings.Join(where, " AND "))
	}
	sb.WriteString("\nLIMIT ?")
	args := append([]any{}, c.args...)
	args = append(args, fuzzyCandidateCap)

	items, err := s.queryItems(ctx, sb.String(), args)
	if err != nil {
		return nil, err
	}

	var out []Result
	for _, it := range items {
		hay := tokenize(it.Filename + " " + it.RelPath)
		if !containsAll(it, mustContain) {
			continue
		}
		score, ok := fuzzyScore(hay, approx)
		if !ok {
			continue
		}
		out = append(out, Result{Item: it, FuzzyMatched: true, Score: score})
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score < out[j].Score
		}
		return out[i].Item.RelPath < out[j].Item.RelPath
	})
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

// fuzzyScore returns the summed best edit distance of every approx term against
// the candidate's tokens, and whether every term found a match within the bound.
func fuzzyScore(tokens, approx []string) (int, bool) {
	total := 0
	for _, term := range approx {
		t := strings.ToLower(term)
		best := fuzzyMaxDistance + 1
		for _, tok := range tokens {
			// A substring hit is distance 0 (covers longer tokens like a filename
			// stem containing the term); otherwise fall back to bounded edit distance.
			if strings.Contains(tok, t) {
				best = 0
				break
			}
			if d := boundedLevenshtein(t, tok, fuzzyMaxDistance); d < best {
				best = d
			}
		}
		if best > fuzzyMaxDistance {
			return 0, false
		}
		total += best
	}
	return total, true
}

// containsAll reports whether every term appears as a (case-insensitive)
// substring of the filename or rel_path (the exact-substring constraint carried
// alongside an explicit ~ term).
func containsAll(it index.Item, terms []string) bool {
	if len(terms) == 0 {
		return true
	}
	hay := strings.ToLower(it.Filename + "\x00" + it.RelPath)
	for _, term := range terms {
		if !strings.Contains(hay, strings.ToLower(term)) {
			return false
		}
	}
	return true
}

// tokenize splits s into lower-cased alphanumeric tokens for edit-distance
// comparison.
func tokenize(s string) []string {
	fields := strings.FieldsFunc(strings.ToLower(s), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsNumber(r)
	})
	return fields
}

// queryItems runs sqlStr with args on the read-only handle and materialises Items.
func (s *Searcher) queryItems(ctx context.Context, sqlStr string, args []any) ([]index.Item, error) {
	rows, err := s.db.QueryContext(ctx, sqlStr, args...)
	if err != nil {
		return nil, fmt.Errorf("query local index: %w", err)
	}
	defer rows.Close()
	var out []index.Item
	for rows.Next() {
		it, err := index.ScanItem(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *it)
	}
	return out, rows.Err()
}
