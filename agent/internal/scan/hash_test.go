package scan

import (
	"os"
	"path/filepath"
	"testing"
)

// Cross-language parity fixtures, re-baselined for QH-T1/QH-T2/QH-T3. The digests
// below were precomputed by running the ACTUAL corrected backend functions
// (filearr.tasks.extract.quick_hash / full_hash) via
// backend/.venv312/Scripts/python.exe over deterministic content
// byte[i] = (i*31 + 7) & 0xFF. They exercise quick_hash's <=64KiB whole-file
// read, the 64-128KiB whole-file read (QH-T1 fix — previously a head-only
// partial read), the ==128KiB boundary (full read, inclusive), and the >128KiB
// head+tail sample; and full_hash as xxh3-128 (QH-T3). Generation command
// (reproducible — imports the real functions, not a reimplementation):
//
//	cd backend && .venv312/Scripts/python.exe - <<'PY'
//	import os, tempfile
//	from filearr.tasks.extract import quick_hash, full_hash
//	def gen(n): return bytes((i*31+7)&0xFF for i in range(n))
//	for n in [0,100,65536,65537,100000,131072,131073,200000]:
//	    fd,name=tempfile.mkstemp()
//	    with os.fdopen(fd,'wb') as f: f.write(gen(n))
//	    print(n, quick_hash(name,n), full_hash(name,n)); os.unlink(name)
//	PY
//
// QuickHash: zeebo/xxh3 default-seed %016x of Sum64 must reproduce Python
// xxh3_64 .hexdigest(). FullHash: %016x%016x of Sum128 (Hi then Lo, big-endian)
// must reproduce Python xxh3_128 .hexdigest() (32 hex chars). If this test fails
// after a dependency bump, the two hashers have diverged and move detection /
// dedupe would silently break.
var xxh3Fixtures = []struct {
	size  int64
	quick string
	full  string
}{
	{0, "2d06800538d394c2", "99aa06d3014798d86001c324468d497f"},
	{100, "8c97158042fbf926", "7f5a1f03462e52b4d61d8dbff22d515f"},
	{65536, "cf188822048798b0", "2c7dfeea59d29a74cf188822048798b0"},
	{65537, "d6b549a3fd1d4112", "a7262c585d065f55d6b549a3fd1d4112"},
	{100000, "ccf90df7e7e37036", "8ce7a24d31cd94b1ccf90df7e7e37036"},
	{131072, "c88c4139bb021d72", "3dddd29aa02945eac88c4139bb021d72"},
	{131073, "5b74c4a4515af86e", "32be7bc8b7759e55b23d37ad8ddf88b5"},
	{200000, "79fae598adb25419", "09d0637de0b290ba2cece8819a5a8009"},
}

// genContent reproduces the Python fixture generator in Go.
func genContent(size int64) []byte {
	b := make([]byte, size)
	for i := range b {
		b[i] = byte((int64(i)*31 + 7) & 0xFF)
	}
	return b
}

func TestQuickHashPythonParity(t *testing.T) {
	dir := t.TempDir()
	for _, f := range xxh3Fixtures {
		p := filepath.Join(dir, "q")
		if err := os.WriteFile(p, genContent(f.size), 0o644); err != nil {
			t.Fatal(err)
		}
		got, err := QuickHash(p, f.size)
		if err != nil {
			t.Fatalf("size=%d: %v", f.size, err)
		}
		if got != f.quick {
			t.Errorf("QuickHash size=%d: got %s, want %s (Python parity)", f.size, got, f.quick)
		}
	}
}

func TestFullHashPythonParity(t *testing.T) {
	dir := t.TempDir()
	for _, f := range xxh3Fixtures {
		p := filepath.Join(dir, "f")
		if err := os.WriteFile(p, genContent(f.size), 0o644); err != nil {
			t.Fatal(err)
		}
		got, err := FullHash(p)
		if err != nil {
			t.Fatalf("size=%d: %v", f.size, err)
		}
		if got != f.full {
			t.Errorf("FullHash size=%d: got %s, want %s (Python parity)", f.size, got, f.full)
		}
	}
}

// TestQuickHashBranchDistinctness asserts the whole-file (<=128KiB) vs head+tail
// (>128KiB) branches behave correctly at the boundary after the QH-T1 fix, so a
// regression is caught independently of the digests. Post-fix, a 65537-byte file
// is hashed IN FULL, so its extra byte past 64KiB makes it differ from a
// 65536-byte file (pre-fix they falsely shared a head-only hash — the bug).
func TestQuickHashBranchDistinctness(t *testing.T) {
	dir := t.TempDir()
	hashOf := func(size int64) string {
		p := filepath.Join(dir, "b")
		if err := os.WriteFile(p, genContent(size), 0o644); err != nil {
			t.Fatal(err)
		}
		h, err := QuickHash(p, size)
		if err != nil {
			t.Fatal(err)
		}
		return h
	}
	if hashOf(65537) == hashOf(65536) {
		t.Error("QH-T1: a 64-128KiB file is now hashed in full, so 65537 must differ from 65536")
	}
	if hashOf(131073) == hashOf(131072) {
		t.Error(">128KiB must add the tail window, so 131073 must differ from 131072")
	}
}

// TestQuickHashBugZoneCoverage is the Go mirror of the brief §2 reproduction
// (Case A + Case D): two files whose FIRST 64 KiB are byte-identical but whose
// bytes past 64 KiB differ, sized inside the 64-128KiB band. Pre-QH-T1 the
// head-only read made their QuickHash collide (a false duplicate); post-fix the
// whole-file read must make them differ. Case D pins the ==131072 boundary
// itself (previously excluded by the `size > 131072` guard).
func TestQuickHashBugZoneCoverage(t *testing.T) {
	dir := t.TempDir()
	quickOf := func(name string, data []byte) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, data, 0o644); err != nil {
			t.Fatal(err)
		}
		h, err := QuickHash(p, int64(len(data)))
		if err != nil {
			t.Fatal(err)
		}
		return h
	}
	// Shared first 64 KiB, differing remainder, in the bug band / at the boundary.
	for _, size := range []int{100000, 131072} {
		a := genContent(int64(size))
		b := make([]byte, size)
		copy(b, a)
		for i := quickChunk; i < size; i++ { // flip every byte past the head window
			b[i] = ^b[i]
		}
		ha := quickOf("a", a)
		hb := quickOf("b", b)
		if ha == hb {
			t.Errorf("size=%d: identical head but differing tail must NOT collide (got %s == %s)", size, ha, hb)
		}
	}
}
