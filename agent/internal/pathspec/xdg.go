package pathspec

import (
	"bufio"
	"strings"
)

// parseUserDirs parses the freedesktop.org `user-dirs.dirs` file body into a
// map of XDG_* variable name → absolute path, substituting a leading `$HOME` with
// the supplied home directory (W6-R1 §2.1). It is pure and OS-independent so the
// locale-translated-folder behavior is table-testable on any host.
//
// The file is shell syntax in practice restricted to `XDG_FOO_DIR="$HOME/Bar"`
// lines (a comment `#...` and blank lines aside). We parse it WITHOUT a shell:
// split on the first `=`, strip surrounding quotes, and expand only a leading
// `$HOME` / `${HOME}` token (the sole substitution the spec uses). A value that
// is already absolute (no `$HOME`) is kept verbatim.
func parseUserDirs(content, home string) map[string]string {
	out := map[string]string{}
	sc := bufio.NewScanner(strings.NewReader(content))
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		eq := strings.IndexByte(line, '=')
		if eq < 0 {
			continue
		}
		key := strings.TrimSpace(line[:eq])
		if !strings.HasPrefix(key, "XDG_") || !strings.HasSuffix(key, "_DIR") {
			continue
		}
		val := strings.TrimSpace(line[eq+1:])
		val = trimQuotes(val)
		if val == "" {
			continue
		}
		out[key] = substHome(val, home)
	}
	return out
}

func trimQuotes(s string) string {
	if len(s) >= 2 {
		if (s[0] == '"' && s[len(s)-1] == '"') || (s[0] == '\'' && s[len(s)-1] == '\'') {
			return s[1 : len(s)-1]
		}
	}
	return s
}

// substHome replaces a leading `$HOME` or `${HOME}` token with home. Only the
// leading position is meaningful for user-dirs.dirs values.
func substHome(val, home string) string {
	switch {
	case val == "$HOME" || val == "${HOME}":
		return home
	case strings.HasPrefix(val, "$HOME/"):
		return home + val[len("$HOME"):]
	case strings.HasPrefix(val, "${HOME}/"):
		return home + val[len("${HOME}"):]
	default:
		return val
	}
}
