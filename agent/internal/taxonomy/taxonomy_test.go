package taxonomy

import (
	"testing"
)

func TestSeedParsesAndClassifies(t *testing.T) {
	seed, err := Seed()
	if err != nil {
		t.Fatalf("seed parse: %v", err)
	}
	if seed.Version() != SeedVersion {
		t.Errorf("seed version = %d, want %d", seed.Version(), SeedVersion)
	}
	// The seed mirrors central's default taxonomy: 1271 extensions, 37 groups, 9
	// categories (matches backend test_taxonomy_w8 counts + the compact payload).
	if n := len(seed.extToGroup); n != 1271 {
		t.Errorf("ext_to_group size = %d, want 1271", n)
	}
	if n := len(seed.groupToCategory); n != 37 {
		t.Errorf("group_to_category size = %d, want 37", n)
	}
	if n := len(seed.categoryExtractor); n != 9 {
		t.Errorf("category_extractor size = %d, want 9", n)
	}

	cases := []struct {
		path         string
		wantCategory string
		wantGroup    string
	}{
		{"Movies/Film.mkv", "video", "video"},
		{"Music/song.flac", "audio", "audio-lossless"},
		{"Music/track.MP3", "audio", "audio-lossy"}, // case-insensitive
		{"Photos/beach.jpg", "image", "raster-photo"},
		{"backup.tar.gz", "archive", "archive"}, // compound wins as a whole
		{"backup.tar.zst", "archive", "archive"},
		{"model.stl", "three-d-cad", "3d-model"},
		{"report.pdf", "document", "pdf"},
		{"README", "other", "other"},      // no extension
		{".bashrc", "other", "other"},     // dotfile, no stem
		{"weird.xyzzy", "other", "other"}, // unmapped extension
	}
	for _, c := range cases {
		gotCat, gotGrp := seed.Classify(c.path)
		if gotCat != c.wantCategory || gotGrp != c.wantGroup {
			t.Errorf("Classify(%q) = (%q, %q), want (%q, %q)",
				c.path, gotCat, gotGrp, c.wantCategory, c.wantGroup)
		}
		if seed.Category(c.path) != c.wantCategory {
			t.Errorf("Category(%q) = %q, want %q", c.path, seed.Category(c.path), c.wantCategory)
		}
		if seed.Group(c.path) != c.wantGroup {
			t.Errorf("Group(%q) = %q, want %q", c.path, seed.Group(c.path), c.wantGroup)
		}
	}
}

func TestSeedPrimaryCategories(t *testing.T) {
	seed := SeedOrEmpty()
	primary := map[string]bool{"image": true, "audio": true, "video": true, "document": true, "three-d-cad": true}
	for cat := range seed.categoryExtractor {
		got := seed.IsPrimaryCategory(cat)
		if got != primary[cat] {
			t.Errorf("IsPrimaryCategory(%q) = %v, want %v", cat, got, primary[cat])
		}
	}
	// A category the taxonomy does not know is never primary.
	if seed.IsPrimaryCategory("nonexistent") {
		t.Error("unknown category must not be primary")
	}
}

func TestSeedExtractorMap(t *testing.T) {
	seed := SeedOrEmpty()
	cases := map[string]string{
		"image": "image", "audio": "audio", "video": "video",
		"document": "document", "three-d-cad": "model3d",
		"development": "", "archive": "", "system": "", "other": "",
	}
	for cat, want := range cases {
		if got := seed.Extractor(cat); got != want {
			t.Errorf("Extractor(%q) = %q, want %q", cat, got, want)
		}
	}
}

func TestParsePayloadRoundTrip(t *testing.T) {
	seed := SeedOrEmpty()
	buf, err := seed.Marshal()
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	got, err := ParsePayload(buf)
	if err != nil {
		t.Fatalf("re-parse: %v", err)
	}
	if got.Version() != seed.Version() {
		t.Errorf("round-trip version = %d, want %d", got.Version(), seed.Version())
	}
	// Classification survives the round-trip.
	for _, p := range []string{"a.mkv", "a.flac", "a.tar.gz", "README"} {
		wc, wg := seed.Classify(p)
		gc, gg := got.Classify(p)
		if wc != gc || wg != gg {
			t.Errorf("round-trip Classify(%q) = (%q,%q), want (%q,%q)", p, gc, gg, wc, wg)
		}
	}
}

func TestParsePayloadRejectsGarbage(t *testing.T) {
	if _, err := ParsePayload([]byte("not json")); err == nil {
		t.Error("expected parse error on garbage")
	}
}

func TestClassifyEditedTaxonomy(t *testing.T) {
	// An operator edit that moves ``mp3`` to a NEW category flows into
	// classification — the whole point of W8-E. Also verify a compound whose group
	// no longer exists falls through to the final extension.
	ext := map[string]string{"mp3": "podcast", "gz": "archive"}
	g2c := map[string]string{"podcast": "media", "archive": "archive"}
	pod := "podcast"
	tax := New(7, ext, g2c, map[string]*string{"media": &pod, "archive": nil}, []string{"media"})
	if c, g := tax.Classify("show.mp3"); c != "media" || g != "podcast" {
		t.Errorf("edited Classify(show.mp3) = (%q,%q), want (media,podcast)", c, g)
	}
	if !tax.IsPrimaryCategory("media") {
		t.Error("edited primary set should include media")
	}
	// tar.gz compound -> archive group still exists -> archive.
	if c, g := tax.Classify("a.tar.gz"); c != "archive" || g != "archive" {
		t.Errorf("Classify(a.tar.gz) = (%q,%q), want (archive,archive)", c, g)
	}
}
