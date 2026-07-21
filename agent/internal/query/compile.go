package query

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/filearr/filearr/agent/internal/taxonomy"
)

// ExecError is a query that PARSES but cannot be executed against the local
// index (the local-index capability boundary — R1). This is NOT a parse error:
// parse succeeds, execution rejects. Code is stable; Keys lists offending filter
// keys where relevant.
type ExecError struct {
	Code    string
	Keys    []string
	Message string
}

func (e *ExecError) Error() string { return e.Message }

// Stable ExecError codes.
const (
	// ErrUnsupportedFilter: a filter that has no column in the local schema
	// (tag / meta.<key> / cf.<name>). The central catalog owns tags and JSONB;
	// the offline agent index deliberately does not (agent/docs/layout.md).
	ErrUnsupportedFilter = "unsupported_filter"
	// ErrUnknownKind: kind:<x> where <x> is not a known file_category.
	ErrUnknownKind = "unknown_kind"
	// ErrUnknownGroup: group:<x> where <x> is not a known file_group.
	ErrUnknownGroup = "unknown_group"
)

// validKinds / validGroups are derived from the BAKED-IN taxonomy seed's
// file_category / file_group vocabularies (W8-E). A “kind:“ filter resolves
// against items.file_category and “group:“ against items.file_group — the agent
// records both at scan time, so no ext→type re-derivation is needed here. The
// seed is the shipped default vocabulary; a locally-added custom category/group
// is not offered here (the local CLI validates against the seed, like central's
// static kind vocabulary before it).
var (
	validKinds  = keySet(taxonomy.SeedOrEmpty().CategoryKeys())
	validGroups = keySet(taxonomy.SeedOrEmpty().GroupKeys())
)

func keySet(keys []string) map[string]bool {
	m := make(map[string]bool, len(keys))
	for _, k := range keys {
		m[k] = true
	}
	return m
}

// compiled is the parameterised translation of a Query against the local index.
type compiled struct {
	where    []string // AND-combined WHERE fragments (never string-built values)
	args     []any
	ftsMatch string   // FTS5 MATCH argument (a single bound parameter); "" if none
	posTerms []string // positive non-fuzzy term texts (candidate-pool + zero-hit fuzzy targets)
	fzTerms  []string // explicit ~ term texts (drive the fuzzy re-rank)
}

// likeEscape escapes LIKE/GLOB-neutral metacharacters for a LIKE pattern so an
// untrusted catalog/query value can never smuggle a wildcard. Used with
// ESCAPE '\'.
func likeEscape(v string) string {
	r := strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`)
	return r.Replace(v)
}

// ftsQuote wraps a term as a single double-quoted FTS5 string token so trigram
// MATCH treats it as a substring probe and DSL/FTS metacharacters cannot break
// out. The whole MATCH string is still passed as a bound ? parameter.
func ftsQuote(v string) string {
	return `"` + strings.ReplaceAll(v, `"`, `""`) + `"`
}

// compile translates a parsed Query into parameterised SQL against the local
// items table. now anchors relative-duration ("age") comparisons; it is a
// parameter so tests are deterministic. Fuzzy (~) terms are NOT emitted as SQL
// predicates (they are a runtime re-rank layer, mirroring the Python
// query_sql.py which rejects fuzzy in SQL context); they are surfaced in
// compiled.fzTerms for searcher.go.
func compile(q Query, now time.Time) (*compiled, *ExecError) {
	c := &compiled{}

	var unsupported []string
	var posFTS []string
	for _, t := range q.Terms {
		if t.Fuzzy {
			c.fzTerms = append(c.fzTerms, t.Value)
			continue // fuzzy terms drive the re-rank, not the WHERE
		}
		if t.Negated {
			// Exclude rows whose filename/rel_path contain the term.
			pat := "%" + likeEscape(t.Value) + "%"
			c.where = append(c.where,
				`NOT (items.filename LIKE ? ESCAPE '\' OR items.rel_path LIKE ? ESCAPE '\')`)
			c.args = append(c.args, pat, pat)
			continue
		}
		posFTS = append(posFTS, ftsQuote(t.Value))
		c.posTerms = append(c.posTerms, t.Value)
	}
	if len(posFTS) > 0 {
		c.ftsMatch = strings.Join(posFTS, " ") // space = AND in FTS5
	}

	for _, f := range q.Filters {
		frag, args, e := compileFilter(f, now)
		if e != nil {
			if e.Code == ErrUnsupportedFilter {
				unsupported = append(unsupported, e.Keys...)
				continue
			}
			return nil, e
		}
		if f.Negated {
			frag = "NOT (" + frag + ")"
		}
		c.where = append(c.where, frag)
		c.args = append(c.args, args...)
	}

	if len(unsupported) > 0 {
		sort.Strings(unsupported)
		return nil, &ExecError{
			Code: ErrUnsupportedFilter,
			Keys: unsupported,
			Message: fmt.Sprintf(
				"filter(s) not supported by the local index (central-only): %s",
				strings.Join(unsupported, ", ")),
		}
	}
	return c, nil
}

// applyScope appends the P7-T4 path-scope allow-list to a compiled query as a
// single OR-combined, parameterised predicate — a row must match at least one
// rel_path GLOB. Empty scope is a no-op (unrestricted). Because the fragment is
// pushed onto c.where/c.args together, both the exact and fuzzy passes inherit it
// (runFuzzy reuses c.where/c.args verbatim), so no fuzzy hit can escape the scope.
// The glob VALUES are always bound parameters — never string-built — so a scope
// predicate cannot smuggle SQL.
func applyScope(c *compiled, scope []string) {
	if len(scope) == 0 {
		return
	}
	ors := make([]string, len(scope))
	for i, g := range scope {
		ors[i] = "items.rel_path GLOB ?"
		c.args = append(c.args, g)
	}
	c.where = append(c.where, "("+strings.Join(ors, " OR ")+")")
}

// compileFilter returns a single WHERE fragment + bound args for one filter.
func compileFilter(f Filter, now time.Time) (string, []any, *ExecError) {
	switch {
	case f.Key == "kind":
		v := f.Value.(StringValue).Value
		if !validKinds[v] {
			return "", nil, &ExecError{
				Code:    ErrUnknownKind,
				Message: fmt.Sprintf("unknown kind (file_category) %q", v),
			}
		}
		return "items.file_category = ?", []any{v}, nil

	case f.Key == "group":
		v := f.Value.(StringValue).Value
		if !validGroups[v] {
			return "", nil, &ExecError{
				Code:    ErrUnknownGroup,
				Message: fmt.Sprintf("unknown group (file_group) %q", v),
			}
		}
		return "items.file_group = ?", []any{v}, nil

	case f.Key == "ext":
		vals := f.Value.(ListValue).Values
		ph := make([]string, len(vals))
		args := make([]any, len(vals))
		for i, v := range vals {
			ph[i] = "?"
			args[i] = v
		}
		return "items.extension IN (" + strings.Join(ph, ",") + ")", args, nil

	case f.Key == "path":
		// R6/task: GLOB on rel_path (the DSL path value is a verbatim glob). This
		// diverges deliberately from central's LIKE-prefix (query_sql.py): the
		// local index honours the glob semantics the grammar documents.
		return "items.rel_path GLOB ?", []any{f.Value.(StringValue).Value}, nil

	case f.Key == "hash":
		v := f.Value.(StringValue).Value
		return "(items.quick_hash = ? OR items.content_hash = ?)", []any{v, v}, nil

	case f.Key == "size":
		frag, args := compileSize(f.Value.(SizeValue))
		return frag, args, nil

	case f.Key == "modified":
		return compileTime("items.mtime_ns", f.Value, now, false)

	case f.Key == "created":
		// first_seen is RFC3339 text; compare on its fixed-width 19-char prefix so
		// lexical ordering is chronological regardless of trailing-fraction width.
		return compileTime("substr(items.first_seen,1,19)", f.Value, now, true)

	case f.Key == "tag" || strings.HasPrefix(f.Key, metaPrefix) || strings.HasPrefix(f.Key, cfPrefix):
		return "", nil, &ExecError{Code: ErrUnsupportedFilter, Keys: []string{f.Key}}
	}
	return "", nil, &ExecError{Code: ErrUnsupportedFilter, Keys: []string{f.Key}}
}

func compileSize(v SizeValue) (string, []any) {
	if v.Op == "range" {
		return "(items.size >= ? AND items.size <= ?)", []any{v.Lo, v.Hi}
	}
	return "items.size " + v.Op + " ?", []any{v.Lo}
}

// compileTime translates a duration/date filter. For durations (relative age),
// it mirrors query_sql.py:_duration_predicate; for dates it mirrors
// _date_predicate. isText=true compares against an RFC3339 19-char prefix
// (created/first_seen); isText=false compares against integer mtime_ns.
func compileTime(col string, v Value, now time.Time, isText bool) (string, []any, *ExecError) {
	switch val := v.(type) {
	case DurationValue:
		// threshold(seconds) = now - age
		thr := func(sec int64) any { return tstamp(now.Add(-time.Duration(sec)*time.Second), isText) }
		if val.Op == "range" {
			// lo..hi are ages; older bound hi -> earlier time.
			return fmt.Sprintf("(%s >= ? AND %s <= ?)", col, col), []any{thr(val.Hi), thr(val.Lo)}, nil
		}
		t := thr(val.Lo)
		switch val.Op {
		case "<": // younger than age -> newer than threshold
			return col + " > ?", []any{t}, nil
		case "<=":
			return col + " >= ?", []any{t}, nil
		case ">": // older than age -> older than threshold
			return col + " < ?", []any{t}, nil
		case ">=":
			return col + " <= ?", []any{t}, nil
		case "=": // within this age window
			return col + " >= ?", []any{t}, nil
		}
	case DateValue:
		d := func(iso string) any { return dstamp(iso, isText, 0) }
		next := func(iso string) any { return dstamp(iso, isText, 1) }
		switch val.Op {
		case "range":
			return fmt.Sprintf("(%s >= ? AND %s < ?)", col, col), []any{d(val.Lo), next(val.Hi)}, nil
		case "=":
			return fmt.Sprintf("(%s >= ? AND %s < ?)", col, col), []any{d(val.Lo), next(val.Lo)}, nil
		case ">":
			return col + " >= ?", []any{next(val.Lo)}, nil
		case ">=":
			return col + " >= ?", []any{d(val.Lo)}, nil
		case "<":
			return col + " < ?", []any{d(val.Lo)}, nil
		case "<=":
			return col + " < ?", []any{next(val.Lo)}, nil
		}
	}
	return "", nil, &ExecError{Code: "bad_time", Message: "unexpected time value"}
}

// tstamp renders an absolute time as the comparison operand for a column: an
// integer nanosecond count (mtime_ns) or an RFC3339 19-char prefix (first_seen).
func tstamp(t time.Time, isText bool) any {
	t = t.UTC()
	if isText {
		return t.Format("2006-01-02T15:04:05")
	}
	return t.UnixNano()
}

// dstamp renders an ISO date (optionally +plusDays) as a column-appropriate
// operand: UTC-midnight nanoseconds, or the 19-char RFC3339 prefix.
func dstamp(iso string, isText bool, plusDays int) any {
	t, err := time.Parse("2006-01-02", iso)
	if err != nil {
		// The parser already validated the calendar date; fall back to zero.
		t = time.Time{}
	}
	t = t.AddDate(0, 0, plusDays).UTC()
	if isText {
		return t.Format("2006-01-02T15:04:05")
	}
	return t.UnixNano()
}
