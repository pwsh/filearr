package scan

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
)

// movePlan is a confirmed, unambiguous rename: keep survivor (the original row,
// id preserved), delete duplicate (the row inserted this scan), repointing the
// survivor at the duplicate's new location. Mirrors move.py:MovePlan.
type movePlan struct {
	survivor  *index.Item
	duplicate *index.Item
}

// moveSentinelPrefix is the U+FFFF-anchored parking prefix for phase-2 renames.
// U+FFFF never appears in a real posix rel_path, so a parked survivor can never
// collide with a scanned path (mirrors move.py's sentinel).
const moveSentinelPrefix = "￿__filearr_move_pending__"

type hashSizeKey struct {
	quick string
	size  int64
}

func moveKey(it *index.Item) (hashSizeKey, bool) {
	if it.QuickHash == "" {
		return hashSizeKey{}, false
	}
	return hashSizeKey{quick: it.QuickHash, size: it.Size}, true
}

// contentMatch is tri-state: 1/-1 when BOTH sides carry a content_hash (equal /
// differ), 0 (unknown) otherwise. Mirrors move._content_match.
func contentMatch(a, b *index.Item) int {
	if a.ContentHash != "" && b.ContentHash != "" {
		if a.ContentHash == b.ContentHash {
			return 1
		}
		return -1
	}
	return 0
}

// planMoves is the pure planner ported from move.plan_moves. candidates =
// vanished original rows carrying a quick_hash; newItems = rows created this scan
// carrying a quick_hash. Returns unambiguous 1:1 renames plus the count of new
// items that had a plausible (quick_hash,size) match but could not be resolved.
func planMoves(candidates, newItems []*index.Item) ([]movePlan, int) {
	candBuckets := map[hashSizeKey][]*index.Item{}
	for _, c := range candidates {
		if k, ok := moveKey(c); ok {
			candBuckets[k] = append(candBuckets[k], c)
		}
	}
	newBuckets := map[hashSizeKey][]*index.Item{}
	var newOrder []hashSizeKey
	for _, n := range newItems {
		if k, ok := moveKey(n); ok {
			if _, seen := newBuckets[k]; !seen {
				newOrder = append(newOrder, k)
			}
			newBuckets[k] = append(newBuckets[k], n)
		}
	}

	var plans []movePlan
	ambiguous := 0
	for _, key := range newOrder {
		news := newBuckets[key]
		cands := candBuckets[key]
		if len(cands) == 0 {
			continue // genuinely new file — no vanished twin
		}
		if len(news) == 1 && len(cands) == 1 {
			n, c := news[0], cands[0]
			if contentMatch(c, n) == -1 { // content_hash VETO
				ambiguous++
				continue
			}
			plans = append(plans, movePlan{survivor: c, duplicate: n})
			continue
		}
		// Multi-way bucket: rescue only pairs content_hash pins to a unique
		// partner on BOTH sides; anything else is ambiguous.
		remaining := append([]*index.Item(nil), cands...)
		for _, n := range news {
			var confirmed []*index.Item
			for _, c := range remaining {
				if contentMatch(c, n) == 1 {
					confirmed = append(confirmed, c)
				}
			}
			if len(confirmed) == 1 {
				c := confirmed[0]
				rivals := 0
				for _, m := range news {
					if m != n && contentMatch(c, m) == 1 {
						rivals++
					}
				}
				if rivals > 0 {
					ambiguous++
					continue
				}
				plans = append(plans, movePlan{survivor: c, duplicate: n})
				remaining = removeItem(remaining, c)
			} else {
				ambiguous++
			}
		}
	}
	return plans, ambiguous
}

func removeItem(s []*index.Item, target *index.Item) []*index.Item {
	for i, v := range s {
		if v == target {
			return append(s[:i], s[i+1:]...)
		}
	}
	return s
}

// detectMoves matches vanished candidates against this scan's new rows and
// transfers identity for unambiguous renames, keeping the survivor's id and
// deleting the duplicate. It replicates move.detect_moves's three-phase
// park-at-sentinel dance so cyclic/swap renames never transiently violate the
// UNIQUE(root_id, rel_path) index. Returns moved/ambiguous counts and the set of
// deleted duplicate ids (so the caller can drop them from new-item bookkeeping).
func detectMoves(ctx context.Context, tx *sql.Tx, libraryRef string, candidates, newItems []*index.Item) (moved, ambiguous int, deletedDupIDs map[string]bool, err error) {
	deletedDupIDs = map[string]bool{}
	if len(candidates) == 0 || len(newItems) == 0 {
		return 0, 0, deletedDupIDs, nil
	}
	plans, ambiguous := planMoves(candidates, newItems)
	if len(plans) == 0 {
		return 0, ambiguous, deletedDupIDs, nil
	}
	now := time.Now().UTC()

	// Capture each survivor's ORIGINAL rel_path (from_rel_path for the wire event)
	// before phase 2 overwrites it with a sentinel and phase 3 with the new path.
	fromRel := make([]string, len(plans))
	for i, p := range plans {
		fromRel[i] = p.survivor.RelPath
	}

	// Phase 1: delete duplicates, freeing the target rel_paths they occupied. No
	// wire event: the duplicate's rel_path is exactly the moved event's target,
	// which the single moved event below authoritatively upserts. (These duplicate
	// hard-deletes are the ONLY DeleteItem call sites in a scan, so suppressing
	// them here never drops a delete central needs.)
	for _, p := range plans {
		if err := index.DeleteItem(ctx, tx, p.duplicate.ID); err != nil {
			return 0, 0, nil, err
		}
		deletedDupIDs[p.duplicate.ID] = true
	}
	// Phase 2: park every survivor at a guaranteed-unique sentinel rel_path so no
	// two survivors (or a survivor vs a lingering original) ever clash. The
	// U+FFFF prefix cannot collide with any real posix rel_path (mirrors move.py).
	for i, p := range plans {
		p.survivor.RelPath = fmt.Sprintf("%s/%d/%s", moveSentinelPrefix, i, p.survivor.ID)
		if err := index.UpdateItem(ctx, tx, p.survivor); err != nil {
			return 0, 0, nil, err
		}
	}
	// Phase 3: rewrite survivors to their final location + freshly-computed hashes.
	// Identity columns (id, first_seen, meta) are deliberately left untouched.
	for i, p := range plans {
		s, d := p.survivor, p.duplicate
		s.RelPath = d.RelPath
		s.Filename = d.Filename
		s.Extension = d.Extension
		s.Size = d.Size
		s.MtimeNs = d.MtimeNs
		s.FileCategory = d.FileCategory
		s.FileGroup = d.FileGroup
		s.Status = d.Status
		s.LastSeen = now
		s.QuickHash = d.QuickHash
		s.ContentHash = d.ContentHash
		if err := index.UpdateItem(ctx, tx, s); err != nil {
			return 0, 0, nil, err
		}
		// Exactly ONE moved event per confirmed rename: from_rel_path = old
		// location, rel_path = new location, payload = the survivor's post-move
		// state. Central applies it as delete(old) + upsert(new) (plan_upserts).
		if err := emit(ctx, tx, libraryRef, outbox.OpMoved, s, fromRel[i], nil); err != nil {
			return 0, 0, nil, err
		}
	}
	return len(plans), ambiguous, deletedDupIDs, nil
}
