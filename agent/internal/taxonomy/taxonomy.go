// Package taxonomy is the agent's local mirror of central's editable File
// Extension Similarity Taxonomy (W8-A/B/E). It replaces the agent's old static
// media-type vocabulary (the removed internal/scan.mediatypes.go) so an agent's
// LOCAL scan gating, classification, and sidecar association honour operator
// taxonomy edits.
//
// A Taxonomy is an immutable snapshot keyed by a central version: flat lookup
// maps (ext->group, group->category, category->extractor) plus the primary
// (sidecar-parent) category set. The Cache (cache.go) loads a snapshot from disk
// on start, falls back to a baked-in SEED (seed.go) when it has never contacted
// central, and version-gates a Refresh off the policy's taxonomy_version.
//
// Classification mirrors central's filearr.taxonomy.Taxonomy.detect / the seed
// filearr.file_groups.detect_group EXACTLY: case-insensitive, extension-only, a
// recognised compound ending (.tar.gz ...) consulted first and winning as a
// whole (-> archive), a name with no usable extension (.bashrc / README)
// resolving to (other, other).
package taxonomy

import (
	"encoding/json"
	"fmt"
	"strings"
)

// CategoryOther / GroupOther are the catch-all keys for an unknown or absent
// extension — identical to central's “other“ sentinels.
const (
	CategoryOther = "other"
	GroupOther    = "other"
)

// SeedVersion is the sentinel version of the baked-in seed snapshot. Real DB
// versions start at 1 (central's migration seeds taxonomy_state.version=1), so 0
// never collides and a seed snapshot is always superseded once central is
// reachable — mirrors filearr.taxonomy._SEED_VERSION.
const SeedVersion = 0

// compoundGroupMap mirrors filearr.file_groups._COMPOUND_GROUP_MAP: recognised
// multi-part endings that classify the WHOLE file as “archive“. This map is
// NOT part of the editable taxonomy (central hardcodes it too), so the agent
// hardcodes it here; the rule only fires when the resolved group still exists in
// the (possibly edited) snapshot.
var compoundGroupMap = func() map[string]string {
	m := map[string]string{}
	for _, w := range []string{"gz", "bz2", "xz", "zst", "zstd", "lz", "lz4", "lzma", "lzo", "z", "br"} {
		m["tar."+w] = "archive"
	}
	return m
}()

// Taxonomy is an immutable classification snapshot for one central version.
type Taxonomy struct {
	version           int
	extToGroup        map[string]string
	groupToCategory   map[string]string
	categoryExtractor map[string]*string // nil pointer == no extractor (JSON null)
	primary           map[string]bool
}

// wirePayload is the compact JSON served by GET /agents/{id}/taxonomy and baked
// into seed.json — see filearr.taxonomy.Taxonomy.agent_payload.
type wirePayload struct {
	Version           int                `json:"version"`
	ExtToGroup        map[string]string  `json:"ext_to_group"`
	GroupToCategory   map[string]string  `json:"group_to_category"`
	CategoryExtractor map[string]*string `json:"category_extractor"`
	PrimaryCategories []string           `json:"primary_categories"`
}

// New builds an immutable snapshot from the compact wire maps. Nil maps are
// tolerated (treated as empty); the result is safe for concurrent reads.
func New(version int, extToGroup, groupToCategory map[string]string, categoryExtractor map[string]*string, primary []string) *Taxonomy {
	t := &Taxonomy{
		version:           version,
		extToGroup:        cloneStr(extToGroup),
		groupToCategory:   cloneStr(groupToCategory),
		categoryExtractor: clonePtr(categoryExtractor),
		primary:           map[string]bool{},
	}
	for _, c := range primary {
		t.primary[c] = true
	}
	return t
}

// ParsePayload decodes the compact agent payload (endpoint body or seed.json)
// into a snapshot. It fails on malformed JSON so a corrupt cache is rejected
// (the caller falls back to the seed).
func ParsePayload(raw []byte) (*Taxonomy, error) {
	var p wirePayload
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, fmt.Errorf("parse taxonomy payload: %w", err)
	}
	return New(p.Version, p.ExtToGroup, p.GroupToCategory, p.CategoryExtractor, p.PrimaryCategories), nil
}

// Marshal renders the snapshot back to the compact wire shape (used to persist a
// fetched snapshot to disk). Keys are emitted in Go map order; the caller does
// not depend on ordering.
func (t *Taxonomy) Marshal() ([]byte, error) {
	primary := make([]string, 0, len(t.primary))
	for c := range t.primary {
		primary = append(primary, c)
	}
	return json.MarshalIndent(wirePayload{
		Version:           t.version,
		ExtToGroup:        t.extToGroup,
		GroupToCategory:   t.groupToCategory,
		CategoryExtractor: t.categoryExtractor,
		PrimaryCategories: primary,
	}, "", "  ")
}

// Version is the central taxonomy version this snapshot represents.
func (t *Taxonomy) Version() int { return t.version }

// Classify resolves a path into (file_category, file_group), mirroring central's
// detect: compound ending first, else the final extension, else (other, other).
// It accepts a full path or a bare filename — only the final component matters.
func (t *Taxonomy) Classify(p string) (category, group string) {
	group = t.groupOf(p)
	if c, ok := t.groupToCategory[group]; ok {
		return c, group
	}
	return CategoryOther, group
}

// Category resolves a path to its file_category (convenience over Classify).
func (t *Taxonomy) Category(p string) string { c, _ := t.Classify(p); return c }

// Group resolves a path to its file_group (convenience over Classify).
func (t *Taxonomy) Group(p string) string { return t.groupOf(p) }

// groupOf implements the extension→group rule (compound-first) shared by central.
func (t *Taxonomy) groupOf(p string) string {
	sufs := lowerSuffixes(filepathBase(p))
	if len(sufs) >= 2 {
		compound := sufs[len(sufs)-2] + "." + sufs[len(sufs)-1]
		if gid, ok := compoundGroupMap[compound]; ok {
			if _, exists := t.groupToCategory[gid]; exists {
				return gid
			}
		}
	}
	if len(sufs) == 0 {
		return GroupOther
	}
	ext := sufs[len(sufs)-1]
	if gid, ok := t.extToGroup[ext]; ok {
		return gid
	}
	return GroupOther
}

// Extractor returns the extraction pipeline a category routes to (or "" for a
// category with no extractor / an unknown category).
func (t *Taxonomy) Extractor(category string) string {
	if p, ok := t.categoryExtractor[category]; ok && p != nil {
		return *p
	}
	return ""
}

// CategoryKeys returns the file_category keys known to this snapshot (unordered).
// Used by the local query surface to validate a “kind:“ filter value.
func (t *Taxonomy) CategoryKeys() []string {
	out := make([]string, 0, len(t.categoryExtractor))
	for k := range t.categoryExtractor {
		out = append(out, k)
	}
	return out
}

// GroupKeys returns the file_group keys known to this snapshot (unordered). Used
// by the local query surface to validate a “group:“ filter value.
func (t *Taxonomy) GroupKeys() []string {
	out := make([]string, 0, len(t.groupToCategory))
	for k := range t.groupToCategory {
		out = append(out, k)
	}
	return out
}

// IsPrimaryCategory reports whether a file_category counts as a "primary"
// (media-ish) sidecar-association parent — the categories central marks primary
// (those with an extractor). Mirrors the old scan.primaryTypes gate, now driven
// by the live taxonomy instead of a hardcoded media-type set.
func (t *Taxonomy) IsPrimaryCategory(category string) bool { return t.primary[category] }

// filepathBase returns the final path component using both separators so a
// Windows-style path classifies the same as a posix one (rel_paths are posix,
// but a raw scan path may be native).
func filepathBase(p string) string {
	if i := strings.LastIndexAny(p, `/\`); i >= 0 {
		return p[i+1:]
	}
	return p
}

// lowerSuffixes mirrors Python's PurePath.suffixes (bare, lower-cased, no dot):
// a trailing-dot name and a leading-dot-only name (“.bashrc“) yield no
// suffixes, so they resolve to “other“ exactly as central does.
func lowerSuffixes(name string) []string {
	if strings.HasSuffix(name, ".") {
		return nil
	}
	name = strings.TrimLeft(name, ".")
	parts := strings.Split(name, ".")
	if len(parts) <= 1 {
		return nil
	}
	out := parts[1:]
	for i := range out {
		out[i] = strings.ToLower(out[i])
	}
	return out
}

func cloneStr(m map[string]string) map[string]string {
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func clonePtr(m map[string]*string) map[string]*string {
	out := make(map[string]*string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}
