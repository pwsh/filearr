// Package query is the agent's local, offline, read-only query surface (P7-T1).
//
// It ports the NORMATIVE query DSL defined once, language-neutrally, by
// backend/filearr/querydsl.py (the Python reference) plus the canonical vectors
// in shared/querydsl-vectors.json (Architect ruling R6). This Go parser must
// pass every one of those vectors byte-for-byte — any Python/Go divergence is a
// release blocker. The AST field names and their ToDict() serialisation mirror
// the Python Query.to_dict() shape so the shared vectors compare cleanly.
//
// The parser is deliberately pure: string in, typed AST or a structured
// ParseError out (no filesystem, no SQLite, no network, no typo-tolerance — that
// is a runtime re-rank layer in searcher.go, not part of the grammar). The only
// error type parse() returns for malformed input is *ParseError.
//
// compile.go translates an AST into parameterised SQL against the P5-T3 local
// index (agent/internal/index/schema.go); searcher.go owns a dedicated
// read-only connection and the R5 fuzzy re-rank layer.
package query

// The AST mirrors backend/filearr/querydsl.py. Each node exposes ToDict()
// returning a map[string]any whose JSON encoding is identical to the Python
// reference's to_dict() — that is what the shared vectors are graded against.

// Value is a parsed filter value (StringValue, ListValue, SizeValue,
// DurationValue, DateValue, or MetaValue). ToDict() yields the typed
// serialisation the vectors assert on.
type Value interface {
	ToDict() map[string]any
}

// StringValue is a plain string filter value (kind / path / tag / hash).
type StringValue struct{ Value string }

func (v StringValue) ToDict() map[string]any {
	return map[string]any{"type": "string", "value": v.Value}
}

// ListValue is a ;-separated list (ext).
type ListValue struct{ Values []string }

func (v ListValue) ToDict() map[string]any {
	// Emit []any so the JSON round-trip in tests matches the vector's decoded shape.
	vals := make([]any, len(v.Values))
	for i, s := range v.Values {
		vals[i] = s
	}
	return map[string]any{"type": "list", "values": vals}
}

// SizeValue is a byte size with a comparator or an inclusive range. Op is one of
// "=" ">" ">=" "<" "<=" "range"; Lo/Hi are bytes (Hi set only for "range").
type SizeValue struct {
	Op string
	Lo int64
	Hi int64
}

func (v SizeValue) ToDict() map[string]any {
	if v.Op == "range" {
		return map[string]any{"type": "size", "op": "range", "lo": v.Lo, "hi": v.Hi}
	}
	return map[string]any{"type": "size", "op": v.Op, "bytes": v.Lo}
}

// DurationValue is a relative age normalised to seconds.
type DurationValue struct {
	Op string
	Lo int64 // seconds
	Hi int64 // seconds, only for op == "range"
}

func (v DurationValue) ToDict() map[string]any {
	if v.Op == "range" {
		return map[string]any{"type": "duration", "op": "range", "lo": v.Lo, "hi": v.Hi}
	}
	return map[string]any{"type": "duration", "op": v.Op, "seconds": v.Lo}
}

// DateValue is an absolute ISO date (YYYY-MM-DD).
type DateValue struct {
	Op string
	Lo string
	Hi string // only for op == "range"
}

func (v DateValue) ToDict() map[string]any {
	if v.Op == "range" {
		return map[string]any{"type": "date", "op": "range", "lo": v.Lo, "hi": v.Hi}
	}
	return map[string]any{"type": "date", "op": v.Op, "iso": v.Lo}
}

// MetaValue is a meta.<key> / cf.<name> operand kept as raw text (the JSONB
// runtime type is unknown at parse time; the SQL translator casts per comparator).
type MetaValue struct {
	Op string
	Lo string
	Hi string // only for op == "range"
}

func (v MetaValue) ToDict() map[string]any {
	if v.Op == "range" {
		return map[string]any{"type": "meta", "op": "range", "lo": v.Lo, "hi": v.Hi}
	}
	return map[string]any{"type": "meta", "op": v.Op, "value": v.Lo}
}

// Term is a free-text term (bare or quoted). Negated marks -/!, Fuzzy marks ~.
type Term struct {
	Value   string
	Negated bool
	Fuzzy   bool
}

func (t Term) ToDict() map[string]any {
	return map[string]any{"value": t.Value, "negated": t.Negated, "fuzzy": t.Fuzzy}
}

// Filter is KEY:value. Key is a known 8-key filter or a dynamic meta./cf. key.
type Filter struct {
	Key     string
	Value   Value
	Negated bool
}

func (f Filter) ToDict() map[string]any {
	return map[string]any{"key": f.Key, "negated": f.Negated, "value": f.Value.ToDict()}
}

// Query is the whole parsed query: free-text terms and typed filters, plus a
// computed Fuzzy flag (true if any free-text term carries ~).
type Query struct {
	Terms   []Term
	Filters []Filter
}

// Fuzzy is the query-level convenience flag: any free-text term carries ~.
func (q Query) Fuzzy() bool {
	for _, t := range q.Terms {
		if t.Fuzzy {
			return true
		}
	}
	return false
}

func (q Query) ToDict() map[string]any {
	terms := make([]any, len(q.Terms))
	for i, t := range q.Terms {
		terms[i] = t.ToDict()
	}
	filters := make([]any, len(q.Filters))
	for i, f := range q.Filters {
		filters[i] = f.ToDict()
	}
	return map[string]any{
		"terms":   terms,
		"filters": filters,
		"fuzzy":   q.Fuzzy(),
	}
}
