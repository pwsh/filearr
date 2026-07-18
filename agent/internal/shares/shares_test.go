package shares

import (
	"os"
	"testing"
	"time"
)

// newTestResolver builds a Resolver over a FIXED export set (no live enumeration),
// with a stable host + case-fold behaviour, so mapping is deterministic on any OS.
func newTestResolver(host string, fold bool, exports []export) *Resolver {
	r := &Resolver{
		host:     host,
		ttl:      time.Minute,
		caseFold: fold,
		now:      time.Now,
		enum:     func() []export { return exports },
	}
	return r
}

func TestHintSMBLongestPrefixWins(t *testing.T) {
	r := newTestResolver("NAS", false, []export{
		{name: "data", path: "/srv/data", kind: "smb"},
		{name: "media", path: "/srv/data/media", kind: "smb"},
	})
	h := r.Hint("/srv/data/media/movies/a.mkv")
	if h == nil {
		t.Fatal("expected a hint")
	}
	if h.ShareURL != "smb://NAS/media/movies/a.mkv" {
		t.Errorf("ShareURL = %q", h.ShareURL)
	}
	if h.UNC != `\\NAS\media\movies\a.mkv` {
		t.Errorf("UNC = %q", h.UNC)
	}
	if h.ShareName != "media" || h.Host != "NAS" || h.Source != "agent" {
		t.Errorf("hint meta wrong: %+v", h)
	}
	// A path only the shorter share covers falls back to it.
	h2 := r.Hint("/srv/data/other/b.mkv")
	if h2 == nil || h2.ShareURL != "smb://NAS/data/other/b.mkv" {
		t.Fatalf("shorter-prefix fallback wrong: %+v", h2)
	}
}

func TestHintExactShareRoot(t *testing.T) {
	r := newTestResolver("h", false, []export{{name: "media", path: "/srv/media", kind: "smb"}})
	h := r.Hint("/srv/media")
	if h == nil || h.ShareURL != "smb://h/media" || h.UNC != `\\h\media` {
		t.Fatalf("exact-root hint wrong: %+v", h)
	}
}

func TestHintNoCoverageIsNil(t *testing.T) {
	r := newTestResolver("h", false, []export{{name: "media", path: "/srv/media", kind: "smb"}})
	if h := r.Hint("/elsewhere/x"); h != nil {
		t.Fatalf("uncovered path must have no hint, got %+v", h)
	}
	// Segment boundary: /srv/media must NOT match /srv/mediabackup.
	if h := r.Hint("/srv/mediabackup/x"); h != nil {
		t.Fatalf("substring (non-segment) path must not match, got %+v", h)
	}
}

func TestHintAmbiguousMultiHomedIsNil(t *testing.T) {
	// Two DISTINCT shares export the SAME directory (multi-homed / re-export).
	// R1 honesty: return no hint rather than guess which network name to use.
	r := newTestResolver("h", false, []export{
		{name: "media", path: "/srv/media", kind: "smb"},
		{name: "movies", path: "/srv/media", kind: "smb"},
	})
	if h := r.Hint("/srv/media/x.mkv"); h != nil {
		t.Fatalf("ambiguous coverage must yield no hint, got %+v", h)
	}
}

func TestHintNFSHasURLButNoUNC(t *testing.T) {
	r := newTestResolver("nfshost", false, []export{{path: "/export/media", kind: "nfs"}})
	h := r.Hint("/export/media/shows/s01e01.mkv")
	if h == nil {
		t.Fatal("expected an nfs hint")
	}
	if h.ShareURL != "nfs://nfshost/export/media/shows/s01e01.mkv" {
		t.Errorf("nfs ShareURL = %q", h.ShareURL)
	}
	if h.UNC != "" || h.ShareName != "" {
		t.Errorf("nfs hint must have no UNC/share name: %+v", h)
	}
}

func TestHintWindowsCaseFoldAndSeparators(t *testing.T) {
	r := newTestResolver("WINBOX", true, []export{{name: "media", path: `D:\Media`, kind: "smb"}})
	// Agent path differs in case + uses backslashes; case-fold match still covers,
	// and the remainder preserves the ORIGINAL filename case.
	h := r.Hint(`d:\media\Movies\A Film.mkv`)
	if h == nil {
		t.Fatal("expected a case-folded match")
	}
	if h.ShareURL != "smb://WINBOX/media/Movies/A Film.mkv" {
		t.Errorf("ShareURL = %q", h.ShareURL)
	}
	if h.UNC != `\\WINBOX\media\Movies\A Film.mkv` {
		t.Errorf("UNC = %q", h.UNC)
	}
}

func TestHintEmptyPathIsNil(t *testing.T) {
	r := newTestResolver("h", false, []export{{name: "m", path: "/srv/m", kind: "smb"}})
	if h := r.Hint(""); h != nil {
		t.Fatalf("empty path must yield nil, got %+v", h)
	}
}

func TestExportsCachedWithinTTL(t *testing.T) {
	calls := 0
	r := &Resolver{host: "h", ttl: time.Minute, now: time.Now, enum: func() []export {
		calls++
		return []export{{name: "m", path: "/srv/m", kind: "smb"}}
	}}
	r.Hint("/srv/m/a")
	r.Hint("/srv/m/b")
	r.Hint("/srv/m/c")
	if calls != 1 {
		t.Fatalf("enumeration should be cached within TTL, called %d times", calls)
	}
}

func TestExportsReenumerateAfterTTL(t *testing.T) {
	calls := 0
	now := time.Unix(0, 0)
	r := &Resolver{host: "h", ttl: time.Minute, now: func() time.Time { return now }, enum: func() []export {
		calls++
		return nil
	}}
	r.exports()
	now = now.Add(2 * time.Minute)
	r.exports()
	if calls != 2 {
		t.Fatalf("expected re-enumeration after TTL, called %d times", calls)
	}
}

// TestLiveEnumerateOptIn exercises the REAL per-OS enumeration only when
// explicitly opted in (it depends on the host's actual shares, so it is never a
// gating test). It merely asserts enumeration does not panic and is honest.
func TestLiveEnumerateOptIn(t *testing.T) {
	if os.Getenv("FILEARR_TEST_LIVE_SHARES") == "" {
		t.Skip("set FILEARR_TEST_LIVE_SHARES=1 to exercise live share enumeration")
	}
	got := enumerateOS()
	t.Logf("live enumeration returned %d export(s): %+v", len(got), got)
}
