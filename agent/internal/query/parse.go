package query

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"unicode"
)

// ParseError is the ONLY error Parse returns for malformed input. Code and
// Position are the R6 contract (asserted by the shared vectors); Reason is
// informational. Position is a 0-based rune index into the input.
type ParseError struct {
	Position int
	Code     string
	Reason   string
}

func (e *ParseError) Error() string {
	return fmt.Sprintf("%s at %d: %s", e.Code, e.Position, e.Reason)
}

func perr(pos int, code, reason string) *ParseError {
	return &ParseError{Position: pos, Code: code, Reason: reason}
}

// --- Vocabulary (mirrors querydsl.py) --------------------------------------

var keys = map[string]bool{
	"kind": true, "ext": true, "size": true, "modified": true,
	"created": true, "path": true, "tag": true, "hash": true,
}

var (
	lowerKeys = map[string]bool{"kind": true, "ext": true, "hash": true}
	listKeys  = map[string]bool{"ext": true}
	sizeKeys  = map[string]bool{"size": true}
	timeKeys  = map[string]bool{"modified": true, "created": true}
	hashKeys  = map[string]bool{"hash": true}
)

var sizeSuffix = map[string]int64{
	"": 1, "K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024, "T": 1024 * 1024 * 1024 * 1024,
}

var durationUnit = map[string]int64{
	"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
}

// META_PREFIX / CF_PREFIX dynamic-filter families and their allow-listed subkey
// charset (see querydsl.py). The subkey becomes a JSONB accessor path, so it is
// validated at parse time — never a bound value.
const (
	metaPrefix       = "meta."
	cfPrefix         = "cf."
	maxDynamicKeyLen = 64
)

var (
	dateRE       = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}$`)
	durationRE   = regexp.MustCompile(`^(\d+)([smhdw])$`)
	hexRE        = regexp.MustCompile(`^[0-9a-f]+$`)
	dynamicKeyRE = regexp.MustCompile(`^[a-z0-9_]+(?:\.[a-z0-9_]+)*$`)
)

// --- Lexer -----------------------------------------------------------------

// lexChar carries a rune, whether it came from inside a quoted span, and its
// original 0-based rune index (used for error positions).
type lexChar struct {
	ch      rune
	inQuote bool
	idx     int
}

func lex(s string) ([][]lexChar, *ParseError) {
	rs := []rune(s)
	var tokens [][]lexChar
	var cur []lexChar
	curStarted := false
	inQuote := false
	quoteStart := -1
	for i := 0; i < len(rs); i++ {
		c := rs[i]
		if inQuote {
			if c == '"' {
				inQuote = false
			} else {
				cur = append(cur, lexChar{c, true, i})
			}
			continue
		}
		if c == '"' {
			curStarted = true
			inQuote = true
			quoteStart = i
			continue
		}
		if unicode.IsSpace(c) {
			if curStarted {
				tokens = append(tokens, cur)
				cur = nil
				curStarted = false
			}
			continue
		}
		curStarted = true
		cur = append(cur, lexChar{c, false, i})
	}
	if inQuote {
		return nil, perr(quoteStart, "unterminated_quote", "unterminated quoted string")
	}
	if curStarted {
		tokens = append(tokens, cur)
	}
	return tokens, nil
}

// --- Helpers (mirrors querydsl.py) -----------------------------------------

// readComparator peels a leading comparator off val; returns ("", val) when absent.
func readComparator(val string) (string, string) {
	if len(val) >= 2 && (val[:2] == ">=" || val[:2] == "<=") {
		return val[:2], val[2:]
	}
	if len(val) >= 1 && (val[0] == '>' || val[0] == '<' || val[0] == '=') {
		return val[:1], val[1:]
	}
	return "", val
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

func parseSizeNum(s string, pos int) (int64, *ParseError) {
	if s == "" {
		return 0, perr(pos, "bad_size_value", "expected a size")
	}
	suffix := ""
	numpart := s
	last := rune(s[len(s)-1])
	if unicode.IsLetter(last) {
		suffix = strings.ToUpper(string(last))
		numpart = s[:len(s)-1]
		if _, ok := sizeSuffix[suffix]; !ok || suffix == "" {
			return 0, perr(pos, "bad_size_suffix", fmt.Sprintf("unknown size suffix %q", string(last)))
		}
	}
	if numpart == "" || !isDigits(numpart) {
		return 0, perr(pos, "bad_size_value", fmt.Sprintf("not an integer size: %q", s))
	}
	n, err := strconv.ParseInt(numpart, 10, 64)
	if err != nil {
		return 0, perr(pos, "bad_size_value", fmt.Sprintf("not an integer size: %q", s))
	}
	return n * sizeSuffix[suffix], nil
}

func parseSize(val string, pos int) (Value, *ParseError) {
	op, rest := readComparator(val)
	if op == "" && strings.Contains(val, "..") {
		parts := strings.Split(val, "..")
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			return nil, perr(pos, "bad_range", fmt.Sprintf("malformed size range: %q", val))
		}
		lo, e := parseSizeNum(parts[0], pos)
		if e != nil {
			return nil, e
		}
		hi, e := parseSizeNum(parts[1], pos)
		if e != nil {
			return nil, e
		}
		return SizeValue{Op: "range", Lo: lo, Hi: hi}, nil
	}
	if op != "" {
		if strings.Contains(rest, "..") {
			return nil, perr(pos, "bad_range", "a range may not carry a comparator")
		}
		n, e := parseSizeNum(rest, pos)
		if e != nil {
			return nil, e
		}
		return SizeValue{Op: op, Lo: n}, nil
	}
	n, e := parseSizeNum(val, pos)
	if e != nil {
		return nil, e
	}
	return SizeValue{Op: "=", Lo: n}, nil
}

// parseTimeAtom returns kind ("date"|"duration"), the ISO string (for date) and
// seconds (for duration).
func parseTimeAtom(s string, pos int) (kind, iso string, seconds int64, e *ParseError) {
	if s == "" {
		return "", "", 0, perr(pos, "bad_time_value", "expected a date or duration")
	}
	if dateRE.MatchString(s) {
		if !validCalendarDate(s) {
			return "", "", 0, perr(pos, "bad_date", fmt.Sprintf("not a valid calendar date: %q", s))
		}
		return "date", s, 0, nil
	}
	if m := durationRE.FindStringSubmatch(s); m != nil {
		n, _ := strconv.ParseInt(m[1], 10, 64)
		return "duration", "", n * durationUnit[m[2]], nil
	}
	return "", "", 0, perr(pos, "bad_time_value", fmt.Sprintf("not a date or duration: %q", s))
}

func parseTime(val string, pos int) (Value, *ParseError) {
	op, rest := readComparator(val)
	if op == "" && strings.Contains(val, "..") {
		parts := strings.Split(val, "..")
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			return nil, perr(pos, "bad_range", fmt.Sprintf("malformed time range: %q", val))
		}
		k1, iso1, sec1, e := parseTimeAtom(parts[0], pos)
		if e != nil {
			return nil, e
		}
		k2, iso2, sec2, e := parseTimeAtom(parts[1], pos)
		if e != nil {
			return nil, e
		}
		if k1 != k2 {
			return nil, perr(pos, "bad_range", "a range must not mix dates and durations")
		}
		if k1 == "date" {
			return DateValue{Op: "range", Lo: iso1, Hi: iso2}, nil
		}
		return DurationValue{Op: "range", Lo: sec1, Hi: sec2}, nil
	}
	if op != "" {
		if strings.Contains(rest, "..") {
			return nil, perr(pos, "bad_range", "a range may not carry a comparator")
		}
		return buildTime(op, rest, pos)
	}
	return buildTime("=", val, pos)
}

func buildTime(op, atom string, pos int) (Value, *ParseError) {
	kind, iso, sec, e := parseTimeAtom(atom, pos)
	if e != nil {
		return nil, e
	}
	if kind == "date" {
		return DateValue{Op: op, Lo: iso}, nil
	}
	return DurationValue{Op: op, Lo: sec}, nil
}

func parseMetaValue(val string, pos int) (Value, *ParseError) {
	op, rest := readComparator(val)
	if op == "" && strings.Contains(val, "..") {
		parts := strings.Split(val, "..")
		if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
			return nil, perr(pos, "bad_range", fmt.Sprintf("malformed range: %q", val))
		}
		return MetaValue{Op: "range", Lo: parts[0], Hi: parts[1]}, nil
	}
	if op != "" {
		if strings.Contains(rest, "..") {
			return nil, perr(pos, "bad_range", "a range may not carry a comparator")
		}
		if rest == "" {
			return nil, perr(pos, "empty_value", "comparator with no value")
		}
		return MetaValue{Op: op, Lo: rest}, nil
	}
	return MetaValue{Op: "=", Lo: val}, nil
}

func isDynamicKey(key string) bool {
	return strings.HasPrefix(key, metaPrefix) || strings.HasPrefix(key, cfPrefix)
}

func validateDynamicKey(key string, pos int) *ParseError {
	prefix, code := metaPrefix, "bad_meta_key"
	if !strings.HasPrefix(key, metaPrefix) {
		prefix, code = cfPrefix, "bad_cf_key"
	}
	sub := key[len(prefix):]
	if sub == "" || len(sub) > maxDynamicKeyLen || !dynamicKeyRE.MatchString(sub) {
		return perr(pos, code, fmt.Sprintf("invalid %skey %q", prefix, sub))
	}
	return nil
}

func parseFilterValue(key, val string, pos int) (Value, *ParseError) {
	if val == "" {
		return nil, perr(pos, "empty_value", fmt.Sprintf("%s: has an empty value", key))
	}
	if isDynamicKey(key) {
		return parseMetaValue(val, pos)
	}
	if lowerKeys[key] {
		val = strings.ToLower(val)
	}
	if listKeys[key] {
		var out []string
		for _, part := range strings.Split(val, ";") {
			part = strings.TrimLeft(part, ".")
			if part == "" {
				return nil, perr(pos, "empty_value", "empty item in list value")
			}
			out = append(out, part)
		}
		return ListValue{Values: out}, nil
	}
	if hashKeys[key] {
		if !hexRE.MatchString(val) {
			return nil, perr(pos, "bad_hash", fmt.Sprintf("not a hex digest: %q", val))
		}
		return StringValue{Value: val}, nil
	}
	if sizeKeys[key] {
		return parseSize(val, pos)
	}
	if timeKeys[key] {
		return parseTime(val, pos)
	}
	return StringValue{Value: val}, nil // kind / path / tag
}

// parseToken parses one lexed token into a *Term or *Filter (or nil for an empty
// token). Exactly one of term/filter is non-nil on success.
func parseToken(pairs []lexChar) (*Term, *Filter, *ParseError) {
	idx := 0
	negated := false
	fuzzy := false
	fuzzyPos := -1
	if idx < len(pairs) && !pairs[idx].inQuote && (pairs[idx].ch == '-' || pairs[idx].ch == '!') {
		negated = true
		idx++
	}
	if idx < len(pairs) && !pairs[idx].inQuote && pairs[idx].ch == '~' {
		fuzzy = true
		fuzzyPos = pairs[idx].idx
		idx++
	}
	rest := pairs[idx:]

	// First unquoted colon splits a filter key from its value.
	colon := -1
	for j := range rest {
		if rest[j].ch == ':' && !rest[j].inQuote {
			colon = j
			break
		}
	}
	if colon > 0 {
		keyChars := rest[:colon]
		var keyB strings.Builder
		keyUnquoted := true
		for _, kc := range keyChars {
			keyB.WriteRune(kc.ch)
			if kc.inQuote {
				keyUnquoted = false
			}
		}
		key := keyB.String()
		keyPos := keyChars[0].idx
		lkey := strings.ToLower(key)
		isKnown := keys[lkey]
		isDynamic := isDynamicKey(key)
		if keyUnquoted && (isKnown || isDynamic) {
			if fuzzy {
				return nil, nil, perr(fuzzyPos, "fuzzy_on_filter",
					"'~' fuzzy marker is only valid on free-text terms")
			}
			if isDynamic {
				if e := validateDynamicKey(key, keyPos); e != nil {
					return nil, nil, e
				}
			}
			storeKey := lkey
			if isDynamic {
				storeKey = key
			}
			colonIndex := rest[colon].idx
			var valB strings.Builder
			for _, vc := range rest[colon+1:] {
				valB.WriteRune(vc.ch)
			}
			fv, e := parseFilterValue(storeKey, valB.String(), colonIndex+1)
			if e != nil {
				return nil, nil, e
			}
			return nil, &Filter{Key: storeKey, Value: fv, Negated: negated}, nil
		}
	}

	var valB strings.Builder
	for _, vc := range rest {
		valB.WriteRune(vc.ch)
	}
	value := valB.String()
	if value == "" {
		return nil, nil, nil
	}
	return &Term{Value: value, Negated: negated, Fuzzy: fuzzy}, nil, nil
}

// Parse parses a query string into a Query AST, returning *ParseError (and only
// that) on malformed input.
func Parse(s string) (Query, *ParseError) {
	tokens, e := lex(s)
	if e != nil {
		return Query{}, e
	}
	q := Query{Terms: []Term{}, Filters: []Filter{}}
	for _, pairs := range tokens {
		term, filter, e := parseToken(pairs)
		if e != nil {
			return Query{}, e
		}
		if filter != nil {
			q.Filters = append(q.Filters, *filter)
		} else if term != nil {
			q.Terms = append(q.Terms, *term)
		}
	}
	return q, nil
}

// validCalendarDate validates a zero-padded YYYY-MM-DD as a real calendar date
// (mirrors datetime.date.fromisoformat). The regex already guarantees the shape.
func validCalendarDate(s string) bool {
	y, _ := strconv.Atoi(s[0:4])
	mo, _ := strconv.Atoi(s[5:7])
	d, _ := strconv.Atoi(s[8:10])
	if mo < 1 || mo > 12 || d < 1 {
		return false
	}
	return d <= daysInMonth(y, mo)
}

func daysInMonth(y, m int) int {
	switch m {
	case 1, 3, 5, 7, 8, 10, 12:
		return 31
	case 4, 6, 9, 11:
		return 30
	case 2:
		if (y%4 == 0 && y%100 != 0) || y%400 == 0 {
			return 29
		}
		return 28
	}
	return 0
}
