package update

import (
	"strconv"
	"strings"
)

// CompareVersions compares two "semver-ish" version strings, returning -1, 0,
// or 1 for a < b, a == b, a > b.
//
// The comparison is deliberately simple and self-contained (documented rather
// than pulling a semver dependency into the agent) — it is only ever used for
// the "is the offered version newer than what I run" decision:
//
//   - a single leading 'v'/'V' is stripped;
//   - build metadata after the first '+' is DISCARDED (never affects ordering);
//   - the string is split into a release part and an optional pre-release part
//     on the first '-';
//   - release parts are split on '.' and compared component-by-component:
//     numeric components compare numerically, a missing component counts as 0,
//     and a non-numeric component falls back to a byte-wise string compare;
//   - when release parts are equal, a version WITH a pre-release ranks BELOW one
//     without (1.2.0-rc1 < 1.2.0, per semver); two pre-releases compare by their
//     raw pre-release strings.
func CompareVersions(a, b string) int {
	ar, ap := splitVersion(a)
	br, bp := splitVersion(b)
	if c := compareRelease(ar, br); c != 0 {
		return c
	}
	// Equal release: pre-release ordering (absence outranks presence).
	switch {
	case ap == "" && bp == "":
		return 0
	case ap == "":
		return 1
	case bp == "":
		return -1
	case ap < bp:
		return -1
	case ap > bp:
		return 1
	default:
		return 0
	}
}

// IsNewer reports whether candidate is strictly newer than current.
func IsNewer(candidate, current string) bool {
	return CompareVersions(candidate, current) > 0
}

// splitVersion normalizes a version into (release, prerelease). Build metadata
// (after '+') is dropped.
func splitVersion(v string) (release, prerelease string) {
	v = strings.TrimSpace(v)
	if len(v) > 0 && (v[0] == 'v' || v[0] == 'V') {
		v = v[1:]
	}
	if i := strings.IndexByte(v, '+'); i >= 0 {
		v = v[:i]
	}
	if i := strings.IndexByte(v, '-'); i >= 0 {
		return v[:i], v[i+1:]
	}
	return v, ""
}

func compareRelease(a, b string) int {
	ap := strings.Split(a, ".")
	bp := strings.Split(b, ".")
	n := len(ap)
	if len(bp) > n {
		n = len(bp)
	}
	for i := 0; i < n; i++ {
		as, bs := "0", "0"
		if i < len(ap) {
			as = ap[i]
		}
		if i < len(bp) {
			bs = bp[i]
		}
		an, aerr := strconv.Atoi(as)
		bn, berr := strconv.Atoi(bs)
		if aerr == nil && berr == nil {
			if an != bn {
				if an < bn {
					return -1
				}
				return 1
			}
			continue
		}
		// Non-numeric component somewhere: fall back to a string compare of it.
		if as != bs {
			if as < bs {
				return -1
			}
			return 1
		}
	}
	return 0
}
