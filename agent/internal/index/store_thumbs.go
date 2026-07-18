package index

import (
	"context"
	"fmt"

	"github.com/filearr/filearr/agent/internal/thumbs"
)

// thumbnailableMediaTypes are the media_type values the P12-T13 pass generates
// for (image + audio family + video). Mirrors thumbs.isThumbnailable; document
// (PDF) is deliberately excluded on the agent (no production-grade pure-Go PDF
// rasterization under CGO_ENABLED=0).
var thumbnailableMediaTypes = []any{"image", "video", "audio", "audiobook", "sample"}

// ThumbCandidates returns every active, non-sidecar, thumbnailable, hashed item
// (joined to its scan-root absolute path) together with its linked sidecar
// children — the input to the thumbnail pass. Satisfies thumbs.Store.
//
// Only hashed items are returned (content_hash OR quick_hash present): the cache
// key derives from the hash, so an un-hashed item has no addressable slot yet and
// is picked up once the next scan sets a hash.
func (s *Store) ThumbCandidates(ctx context.Context) ([]thumbs.Candidate, error) {
	// One query for the sidecar children (grouped by parent), one for candidates.
	sidecars, err := s.sidecarChildren(ctx)
	if err != nil {
		return nil, err
	}

	placeholders := ""
	args := make([]any, 0, len(thumbnailableMediaTypes))
	for i, mt := range thumbnailableMediaTypes {
		if i > 0 {
			placeholders += ","
		}
		placeholders += "?"
		args = append(args, mt)
	}
	q := `
SELECT i.id, r.path, i.rel_path, i.media_type,
       COALESCE(i.content_hash, ''), COALESCE(i.quick_hash, '')
FROM items i JOIN roots r ON r.id = i.root_id
WHERE i.status = 'active' AND i.is_sidecar = 0
  AND i.media_type IN (` + placeholders + `)
  AND (i.content_hash IS NOT NULL OR i.quick_hash IS NOT NULL)`
	rows, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return nil, fmt.Errorf("query thumb candidates: %w", err)
	}
	defer rows.Close()

	var out []thumbs.Candidate
	for rows.Next() {
		var c thumbs.Candidate
		if err := rows.Scan(&c.ItemID, &c.RootPath, &c.RelPath, &c.MediaType, &c.ContentHash, &c.QuickHash); err != nil {
			return nil, err
		}
		c.SidecarRels = sidecars[c.ItemID]
		out = append(out, c)
	}
	return out, rows.Err()
}

// sidecarChildren returns a map of parent item id -> its sidecar children's
// rel_paths (a single query, so the candidate walk is not N+1).
func (s *Store) sidecarChildren(ctx context.Context) (map[string][]string, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT sidecar_of, rel_path FROM items
		 WHERE is_sidecar = 1 AND sidecar_of IS NOT NULL AND sidecar_of != ''`)
	if err != nil {
		return nil, fmt.Errorf("query sidecars: %w", err)
	}
	defer rows.Close()
	out := map[string][]string{}
	for rows.Next() {
		var parent, rel string
		if err := rows.Scan(&parent, &rel); err != nil {
			return nil, err
		}
		out[parent] = append(out[parent], rel)
	}
	return out, rows.Err()
}

// ThumbMarkers returns the (tier → cache_key) markers recorded for an item.
// Satisfies thumbs.Store.
func (s *Store) ThumbMarkers(ctx context.Context, itemID string) ([]thumbs.Marker, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT tier, cache_key FROM thumb_markers WHERE item_id = ?`, itemID)
	if err != nil {
		return nil, fmt.Errorf("query thumb markers: %w", err)
	}
	defer rows.Close()
	var out []thumbs.Marker
	for rows.Next() {
		var m thumbs.Marker
		if err := rows.Scan(&m.Tier, &m.CacheKey); err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// MarkThumb upserts the marker recording that (itemID, tier) was uploaded to
// central under cacheKey. Satisfies thumbs.Store.
func (s *Store) MarkThumb(ctx context.Context, itemID string, tier int, cacheKey string) error {
	_, err := s.db.ExecContext(ctx, `
INSERT INTO thumb_markers(item_id, tier, cache_key, uploaded_at)
VALUES(?, ?, ?, ?)
ON CONFLICT(item_id, tier) DO UPDATE SET
    cache_key   = excluded.cache_key,
    uploaded_at = excluded.uploaded_at`,
		itemID, tier, cacheKey, nowUTC())
	if err != nil {
		return fmt.Errorf("mark thumb: %w", err)
	}
	return nil
}
