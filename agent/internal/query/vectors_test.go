package query

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"testing"
)

// The R6 contract: the Go parser must satisfy every vector in
// shared/querydsl-vectors.json byte-for-byte against the Python reference
// (backend/filearr/querydsl.py). "ast" is the expected Query.ToDict(); "error"
// is the expected ParseError (code + position are the contract; reason is
// informational). The P11-T2 meta./cf. extension grew the file to 81 vectors;
// W8-D added the group: keyword (4 vectors) -> 85. This test asserts that count
// and targets the FILE, not the (stale) task doc.
const expectedVectorCount = 85

type vector struct {
	Name  string         `json:"name"`
	Input string         `json:"input"`
	AST   map[string]any `json:"ast"`
	Error *struct {
		Position int    `json:"position"`
		Code     string `json:"code"`
		Reason   string `json:"reason"`
	} `json:"error"`
}

// vectorsPath walks up from this test file to the repo root (the dir holding
// shared/querydsl-vectors.json) so the test resolves independent of CWD.
func vectorsPath(t *testing.T) string {
	t.Helper()
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot resolve test file path")
	}
	dir := filepath.Dir(thisFile)
	for i := 0; i < 12; i++ {
		cand := filepath.Join(dir, "shared", "querydsl-vectors.json")
		if _, err := os.Stat(cand); err == nil {
			return cand
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	t.Fatal("could not locate shared/querydsl-vectors.json above test file")
	return ""
}

func loadVectors(t *testing.T) []vector {
	t.Helper()
	buf, err := os.ReadFile(vectorsPath(t))
	if err != nil {
		t.Fatalf("read vectors: %v", err)
	}
	var doc struct {
		Version int      `json:"version"`
		Vectors []vector `json:"vectors"`
	}
	if err := json.Unmarshal(buf, &doc); err != nil {
		t.Fatalf("parse vectors: %v", err)
	}
	if doc.Version != 1 {
		t.Fatalf("unexpected vector schema version %d", doc.Version)
	}
	return doc.Vectors
}

func TestVectorCount(t *testing.T) {
	vs := loadVectors(t)
	if len(vs) != expectedVectorCount {
		t.Fatalf("vector count drift: got %d, expected %d (update the constant AND re-verify parity)", len(vs), expectedVectorCount)
	}
	seen := map[string]bool{}
	for _, v := range vs {
		if seen[v.Name] {
			t.Errorf("duplicate vector name %q", v.Name)
		}
		seen[v.Name] = true
	}
}

func TestVectorParity(t *testing.T) {
	vs := loadVectors(t)
	okCount, errCount := 0, 0
	for _, v := range vs {
		v := v
		t.Run(v.Name, func(t *testing.T) {
			q, e := Parse(v.Input)
			if v.Error != nil {
				if e == nil {
					t.Fatalf("expected error %s at %d, got AST", v.Error.Code, v.Error.Position)
				}
				if e.Code != v.Error.Code {
					t.Errorf("code: got %q, want %q", e.Code, v.Error.Code)
				}
				if e.Position != v.Error.Position {
					t.Errorf("position: got %d, want %d", e.Position, v.Error.Position)
				}
				return
			}
			if e != nil {
				t.Fatalf("unexpected error: %v", e)
			}
			// Normalise our AST through a JSON round-trip so numbers/collections
			// decode identically to the vector's decoded shape, then DeepEqual.
			got := normalize(t, q.ToDict())
			want := normalize(t, v.AST)
			if !reflect.DeepEqual(got, want) {
				t.Errorf("AST mismatch\n got: %#v\nwant: %#v", got, want)
			}
		})
		if v.Error != nil {
			errCount++
		} else {
			okCount++
		}
	}
	t.Logf("vectors: %d total (%d ast, %d error)", len(vs), okCount, errCount)
}

func normalize(t *testing.T, v any) any {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out any
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return out
}
