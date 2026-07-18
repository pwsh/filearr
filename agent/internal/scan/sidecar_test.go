package scan

import "testing"

func TestClassifySidecars(t *testing.T) {
	cases := []struct {
		rel        string
		wantKind   string // "" = not a sidecar
		wantParent string // parent stem; "-" = directory-level (HasParent false)
	}{
		// JRiver
		{"Music/Track_JRSidecar.xml", "jriver", "Track"},
		{"Music/_JRSidecar.xml", "jriver", "-"},
		// .nfo — dir-level bare stems vs per-item
		{"Movies/Film/movie.nfo", "nfo", "-"},
		{"Shows/tvshow.nfo", "nfo", "-"},
		{"Movies/Film (2020).nfo", "nfo", "Film (2020)"},
		// .xmp/.thm stem-locked
		{"Photos/IMG_1234.xmp", "xmp", "IMG_1234"},
		{"Video/MVI_5678.thm", "artwork", "MVI_5678"},
		{".xmp", "", ""}, // bare dotfile: ext empty, not a sidecar
		// artwork: directory-level names
		{"Movies/Film/poster.jpg", "artwork", "-"},
		{"Music/Album/folder.jpeg", "artwork", "-"},
		// artwork: per-stem suffixes (the -poster/-thumb branch fires before the
		// season-poster branch, so "season01-poster.jpg" is a per-stem sidecar of
		// "season01" — matches central sidecar.py exactly).
		{"Movies/Film (2020)-thumb.jpg", "artwork", "Film (2020)"},
		{"Movies/Film (2020)-poster.png", "artwork", "Film (2020)"},
		{"Shows/S/season01-poster.jpg", "artwork", "season01"},
		// season poster WITHOUT a hyphen suffix -> directory-level (branch 3c).
		{"Shows/S/season1poster.jpg", "artwork", "-"},
		// non-sidecars
		{"Movies/Film (2020).mkv", "", ""},
		{"Music/song.flac", "", ""},
		{"Docs/report.pdf", "", ""},
	}
	for _, c := range cases {
		info := classify(c.rel)
		if c.wantKind == "" {
			if info != nil {
				t.Errorf("%q: expected non-sidecar, got kind=%q", c.rel, info.Kind)
			}
			continue
		}
		if info == nil {
			t.Errorf("%q: expected sidecar kind=%q, got nil", c.rel, c.wantKind)
			continue
		}
		if info.Kind != c.wantKind {
			t.Errorf("%q: kind=%q, want %q", c.rel, info.Kind, c.wantKind)
		}
		if c.wantParent == "-" {
			if info.HasParent {
				t.Errorf("%q: expected directory-level (no parent), got parent=%q", c.rel, info.ParentStem)
			}
		} else {
			if !info.HasParent || info.ParentStem != c.wantParent {
				t.Errorf("%q: parent=%q(has=%v), want %q", c.rel, info.ParentStem, info.HasParent, c.wantParent)
			}
		}
	}
}
