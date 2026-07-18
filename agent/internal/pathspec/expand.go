package pathspec

import (
	"fmt"
	"strings"
)

// expandTokens substitutes env tokens in a spec: a leading `~` (home), Windows
// `%VAR%`, and POSIX `$VAR` / `${VAR}`. An unset variable is a hard error so a
// spec never degrades into a walk of a stray literal like `%UNSET%\Documents`.
//
// Both env syntaxes are honored regardless of OS so a mixed-authoring operator is
// not surprised — a Windows agent still resolves a `$HOME`-style spec if that is
// what a policy carried, and vice versa.
func expandTokens(spec string, getenv func(string) (string, bool), home func() (string, error)) (string, error) {
	out := spec
	// Leading `~` → home. Only a bare `~` or `~/`... / `~\`... form (never `~user`,
	// which we do not resolve) is expanded.
	if out == "~" || strings.HasPrefix(out, "~/") || strings.HasPrefix(out, `~\`) {
		h, err := home()
		if err != nil {
			return "", fmt.Errorf("resolve ~: %w", err)
		}
		out = h + out[1:]
	}
	var err error
	out, err = expandPercent(out, getenv)
	if err != nil {
		return "", err
	}
	out, err = expandDollar(out, getenv)
	if err != nil {
		return "", err
	}
	return out, nil
}

// expandPercent resolves Windows `%VAR%` tokens. A lone `%` (no closing pair) is
// left verbatim. `%%` is not special-cased (unusual in a path); each `%VAR%` pair
// must name a set variable or the spec fails.
func expandPercent(s string, getenv func(string) (string, bool)) (string, error) {
	var b strings.Builder
	for i := 0; i < len(s); {
		if s[i] != '%' {
			b.WriteByte(s[i])
			i++
			continue
		}
		end := strings.IndexByte(s[i+1:], '%')
		if end < 0 {
			// No closing percent — emit the rest literally.
			b.WriteString(s[i:])
			break
		}
		name := s[i+1 : i+1+end]
		if name == "" {
			// `%%` → a literal percent.
			b.WriteByte('%')
			i += 2
			continue
		}
		val, ok := getenv(name)
		if !ok {
			return "", fmt.Errorf("unset environment variable %%%s%%", name)
		}
		b.WriteString(val)
		i += end + 2
	}
	return b.String(), nil
}

// expandDollar resolves POSIX `$VAR` and `${VAR}` tokens. A `$` not starting a
// valid name is emitted literally.
func expandDollar(s string, getenv func(string) (string, bool)) (string, error) {
	var b strings.Builder
	for i := 0; i < len(s); {
		if s[i] != '$' {
			b.WriteByte(s[i])
			i++
			continue
		}
		if i+1 < len(s) && s[i+1] == '{' {
			end := strings.IndexByte(s[i+2:], '}')
			if end < 0 {
				return "", fmt.Errorf("unterminated ${...} in %q", s)
			}
			name := s[i+2 : i+2+end]
			if name == "" {
				return "", fmt.Errorf("empty ${} in %q", s)
			}
			val, ok := getenv(name)
			if !ok {
				return "", fmt.Errorf("unset environment variable ${%s}", name)
			}
			b.WriteString(val)
			i += end + 3
			continue
		}
		// $VAR: name is [A-Za-z_][A-Za-z0-9_]*.
		j := i + 1
		for j < len(s) && isNameByte(s[j], j == i+1) {
			j++
		}
		if j == i+1 {
			// Bare `$` — literal.
			b.WriteByte('$')
			i++
			continue
		}
		name := s[i+1 : j]
		val, ok := getenv(name)
		if !ok {
			return "", fmt.Errorf("unset environment variable $%s", name)
		}
		b.WriteString(val)
		i = j
	}
	return b.String(), nil
}

func isNameByte(c byte, first bool) bool {
	if c == '_' || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') {
		return true
	}
	if !first && c >= '0' && c <= '9' {
		return true
	}
	return false
}
