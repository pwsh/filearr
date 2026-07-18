package update

import "testing"

func TestCompareVersions(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"1.0.0", "1.0.0", 0},
		{"1.0.1", "1.0.0", 1},
		{"1.0.0", "1.0.1", -1},
		{"1.2.0", "1.10.0", -1}, // numeric, not lexical
		{"2.0.0", "1.9.9", 1},
		{"v1.4.0", "1.4.0", 0},       // leading v stripped
		{"1.4", "1.4.0", 0},          // missing component == 0
		{"1.4.0", "1.4", 0},          //
		{"1.4.0+build9", "1.4.0", 0}, // build metadata ignored
		{"1.4.0-rc1", "1.4.0", -1},   // prerelease ranks below release
		{"1.4.0", "1.4.0-rc1", 1},
		{"1.4.0-rc2", "1.4.0-rc1", 1}, // prerelease lexical
	}
	for _, c := range cases {
		if got := CompareVersions(c.a, c.b); got != c.want {
			t.Errorf("CompareVersions(%q,%q)=%d want %d", c.a, c.b, got, c.want)
		}
	}
}

func TestIsNewer(t *testing.T) {
	if !IsNewer("1.4.1", "1.4.0") {
		t.Error("1.4.1 should be newer than 1.4.0")
	}
	if IsNewer("1.4.0", "1.4.0") {
		t.Error("equal versions are not newer")
	}
	if IsNewer("0.0.0-dev", "1.4.0") {
		t.Error("dev build should not be newer than a release")
	}
}
