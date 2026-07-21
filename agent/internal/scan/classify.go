package scan

import (
	"path/filepath"
	"strings"

	"github.com/filearr/filearr/agent/internal/taxonomy"
)

// Classifier resolves a path into the File Extension Similarity Taxonomy
// (file_category, file_group) and reports which categories are "primary" sidecar
// parents. It is satisfied by *taxonomy.Taxonomy (the process-shared cache's
// current snapshot), so an operator taxonomy edit flows into local scan gating,
// classification, and sidecar association. Taken as an interface so scan stays
// testable with a hand-rolled classifier.
type Classifier interface {
	Classify(path string) (category, group string)
	IsPrimaryCategory(category string) bool
}

// seedClassifier is the fallback used when Options.Taxonomy is nil (standalone
// scans / tests): the baked-in default taxonomy, so classification never
// no-ops. A live daemon always injects the cache snapshot instead.
func seedClassifier() Classifier { return taxonomy.SeedOrEmpty() }

// categoryEnabled mirrors central's library gating model (W8-E): a file is
// included iff BOTH allow-lists are empty (no restriction) OR its category is in
// enabledCategories OR its group is in enabledGroups. Sidecars bypass this gate
// entirely (handled at the call site).
func categoryEnabled(enabledCats, enabledGroups map[string]bool, category, group string) bool {
	if len(enabledCats) == 0 && len(enabledGroups) == 0 {
		return true
	}
	return enabledCats[category] || enabledGroups[group]
}

// fileExtension returns the bare lower-case extension (no dot) for storage in
// Item.extension, or "" when absent. Mirrors os.path.splitext(...).lstrip(".").
func fileExtension(name string) string {
	return strings.ToLower(strings.TrimPrefix(filepath.Ext(name), "."))
}
