package scan

import (
	"context"
	"path"
	"strings"

	"github.com/filearr/filearr/agent/internal/index"
)

// SidecarStats reports what the association pass did. Mirrors the subset of
// associate_sidecars' return the agent tracks (NFO metadata folding is a central
// extraction feature and is out of scope for the local index — documented
// deviation).
type SidecarStats struct {
	Sidecars int
	Linked   int
}

type dirStemKey struct {
	dir  string
	stem string
}

// resolveLinks is the pure planner ported from associate.resolve_links: given the
// active items for a root, returns {sidecarID: parentID} ("" parent = unresolved).
// Only sidecars appear as keys. "Primary" (media-ish) parents are now decided by
// the taxonomy classifier's IsPrimaryCategory (the categories with an extractor),
// so operator taxonomy edits flow into sidecar association (W8-E).
func resolveLinks(items []*index.Item, classifier Classifier) map[string]string {
	byDirStem := map[dirStemKey]*index.Item{}
	dirPrimaries := map[string][]*index.Item{}
	classified := map[string]*sidecarInfo{}

	for _, it := range items {
		info := classify(it.RelPath)
		classified[it.ID] = info
		if info == nil { // a real media file — eligible parent
			key := dirStemKey{dir: dirOf(it.RelPath), stem: stemOf(it.RelPath)}
			primary := classifier.IsPrimaryCategory(it.FileCategory)
			existing := byDirStem[key]
			// Prefer a primary category on stem collision (e.g. .mkv over .srt).
			if existing == nil || (primary && !classifier.IsPrimaryCategory(existing.FileCategory)) {
				byDirStem[key] = it
			}
			if primary {
				d := dirOf(it.RelPath)
				dirPrimaries[d] = append(dirPrimaries[d], it)
			}
		}
	}

	primaryFor := func(dir string) *index.Item {
		cands := dirPrimaries[dir]
		if len(cands) == 0 {
			return nil
		}
		// Deterministic: largest file, tie-break on rel_path.
		best := cands[0]
		for _, c := range cands[1:] {
			if c.Size > best.Size || (c.Size == best.Size && c.RelPath > best.RelPath) {
				best = c
			}
		}
		return best
	}

	links := map[string]string{}
	for _, it := range items {
		info := classified[it.ID]
		if info == nil {
			continue
		}
		var parent *index.Item
		if info.HasParent {
			key := dirStemKey{dir: info.Directory, stem: strings.ToLower(info.ParentStem)}
			parent = byDirStem[key]
			if parent == nil {
				parent = primaryFor(info.Directory) // fall back to directory primary
			}
		} else {
			parent = primaryFor(info.Directory)
		}
		if parent != nil && parent.ID != it.ID {
			links[it.ID] = parent.ID
		} else {
			links[it.ID] = ""
		}
	}
	return links
}

// associateSidecars recomputes sidecar_of for every active item of a root from
// scratch (idempotent) and persists only the changed FKs. Ported from
// associate.associate_sidecars (minus NFO metadata folding). Runs after move
// detection so it sees surviving ids.
func associateSidecars(ctx context.Context, store *index.Store, rootID string, classifier Classifier) (SidecarStats, error) {
	existing, err := store.LoadItems(ctx, rootID)
	if err != nil {
		return SidecarStats{}, err
	}
	var active []*index.Item
	byID := map[string]*index.Item{}
	for _, it := range existing {
		if it.Status == index.StatusActive {
			active = append(active, it)
			byID[it.ID] = it
		}
	}
	links := resolveLinks(active, classifier)

	var stats SidecarStats
	tx, err := store.Begin(ctx)
	if err != nil {
		return SidecarStats{}, err
	}
	defer func() {
		if tx != nil {
			_ = tx.Rollback()
		}
	}()

	for sid, pid := range links {
		sidecar := byID[sid]
		stats.Sidecars++
		if pid != "" {
			stats.Linked++
		}
		if sidecar.SidecarOf != pid { // update FK only on change (cheap rescans)
			sidecar.SidecarOf = pid
			if err := index.UpdateItem(ctx, tx, sidecar); err != nil {
				return SidecarStats{}, err
			}
		}
	}

	c := tx
	tx = nil
	if err := c.Commit(); err != nil {
		return SidecarStats{}, err
	}
	return stats, nil
}

// dirOf mirrors os.path.dirname over a posix rel path ("" for a top-level file).
func dirOf(relPath string) string {
	d := path.Dir(relPath)
	if d == "." {
		return ""
	}
	return d
}

// stemOf mirrors os.path.splitext(os.path.basename(rel))[0].lower().
func stemOf(relPath string) string {
	base := path.Base(relPath)
	return strings.ToLower(base[:len(base)-len(pathExt(base))])
}
