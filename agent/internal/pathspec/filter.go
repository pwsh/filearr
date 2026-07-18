package pathspec

import (
	"fmt"
	"regexp"
)

// Filter is a compiled include/exclude rel-path matcher (RE2 / Go regexp), the
// per-entry refinement layered on top of the exclusion-preset bundles the walk
// already applies. Exclude always wins; an empty include set means "admit
// everything the excludes did not drop".
type Filter struct {
	include []*regexp.Regexp
	exclude []*regexp.Regexp
}

// CompileFilter compiles include/exclude regexp lists. A bad pattern is a hard
// error (the command is refused rather than silently mis-filtering). Central runs
// a Python `re` sanity gate on the same strings, but RE2 is the authority — a
// pattern that survives the gate yet fails here surfaces as this error.
func CompileFilter(include, exclude []string) (*Filter, error) {
	f := &Filter{}
	var err error
	if f.include, err = compileAll(include, "include_regex"); err != nil {
		return nil, err
	}
	if f.exclude, err = compileAll(exclude, "exclude_regex"); err != nil {
		return nil, err
	}
	return f, nil
}

func compileAll(pats []string, field string) ([]*regexp.Regexp, error) {
	out := make([]*regexp.Regexp, 0, len(pats))
	for i, p := range pats {
		re, err := regexp.Compile(p)
		if err != nil {
			return nil, fmt.Errorf("%s[%d] %q: %w", field, i, p, err)
		}
		out = append(out, re)
	}
	return out, nil
}

// Allow reports whether rel (a root-relative, posix-separated path) passes the
// filter: rejected if any exclude matches; otherwise admitted iff the include set
// is empty OR at least one include matches.
func (f *Filter) Allow(rel string) bool {
	if f == nil {
		return true
	}
	for _, re := range f.exclude {
		if re.MatchString(rel) {
			return false
		}
	}
	if len(f.include) == 0 {
		return true
	}
	for _, re := range f.include {
		if re.MatchString(rel) {
			return true
		}
	}
	return false
}
