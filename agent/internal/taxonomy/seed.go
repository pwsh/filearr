package taxonomy

import (
	_ "embed"
	"sync"
)

// seedJSON is the baked-in DEFAULT taxonomy — the compact agent payload of
// central's SEED (filearr.taxonomy._seed_snapshot().agent_payload()), version 0.
// It lets a never-contacted / offline agent classify correctly with no network.
//
// PROVENANCE + REGENERATION: seed.json is GENERATED from the backend's pure seed
// (backend/filearr/file_groups.py, via filearr.taxonomy). It is NOT hand-edited.
// Regenerate after any change to the central seed taxonomy:
//
//	cd backend && ./.venv312/Scripts/python.exe -c "import json; from filearr \
//	  import taxonomy; open('../agent/internal/taxonomy/seed.json','w', \
//	  encoding='utf-8',newline='\n').write(json.dumps( \
//	  taxonomy._seed_snapshot().agent_payload(),indent=2,sort_keys=True, \
//	  ensure_ascii=False)+'\n')"
//
// A test (seed_test.go) asserts the embedded seed parses and classifies known
// extensions, so a drifted/corrupt regeneration is caught in CI.
//
//go:embed seed.json
var seedJSON []byte

var (
	seedOnce sync.Once
	seedSnap *Taxonomy
	seedErr  error
)

// Seed returns the baked-in default snapshot (parsed once). It never fails at
// runtime for a valid committed seed.json; a parse error is surfaced so tests
// catch a corrupt regeneration, and callers that cannot handle it fall back to
// an empty snapshot via SeedOrEmpty.
func Seed() (*Taxonomy, error) {
	seedOnce.Do(func() {
		seedSnap, seedErr = ParsePayload(seedJSON)
	})
	return seedSnap, seedErr
}

// SeedOrEmpty returns the baked-in seed, or an empty (all-“other“) snapshot if
// the embedded seed somehow fails to parse — so classification never panics.
func SeedOrEmpty() *Taxonomy {
	if s, err := Seed(); err == nil {
		return s
	}
	return New(SeedVersion, nil, nil, nil, nil)
}
