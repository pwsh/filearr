package index

import (
	"database/sql"
	"time"
)

// selectItemColumns is the canonical projection consumed by scanItem. Kept in
// one place so LoadItems and Search stay column-aligned.
const selectItemColumns = `
SELECT id, root_id, rel_path, filename, extension, size, mtime_ns,
       quick_hash, content_hash, file_category, file_group, meta, status, is_sidecar,
       sidecar_of, first_seen, last_seen, synced_at, local_seq_no
FROM items`

// rowScanner is satisfied by both *sql.Row and *sql.Rows.
type rowScanner interface {
	Scan(dest ...any) error
}

// scanItem materialises one Item, mapping SQL NULLs to "" / nil.
func scanItem(r rowScanner) (*Item, error) {
	var (
		it        Item
		ext       sql.NullString
		quick     sql.NullString
		content   sql.NullString
		category  sql.NullString
		group     sql.NullString
		meta      sql.NullString
		sidecarOf sql.NullString
		firstSeen string
		lastSeen  string
		syncedAt  sql.NullString
		isSidecar int64
	)
	if err := r.Scan(
		&it.ID, &it.RootID, &it.RelPath, &it.Filename, &ext, &it.Size, &it.MtimeNs,
		&quick, &content, &category, &group, &meta, &it.Status, &isSidecar,
		&sidecarOf, &firstSeen, &lastSeen, &syncedAt, &it.LocalSeqNo,
	); err != nil {
		return nil, err
	}
	it.Extension = ext.String
	it.QuickHash = quick.String
	it.ContentHash = content.String
	it.FileCategory = category.String
	it.FileGroup = group.String
	it.Meta = meta.String
	it.SidecarOf = sidecarOf.String
	it.IsSidecar = isSidecar != 0
	it.FirstSeen = parseTS(firstSeen)
	it.LastSeen = parseTS(lastSeen)
	if syncedAt.Valid {
		t := parseTS(syncedAt.String)
		it.SyncedAt = &t
	}
	return &it, nil
}

// nullStr maps "" to a SQL NULL and any other value to itself.
func nullStr(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// nullTS maps a nil time pointer to SQL NULL, else RFC3339Nano UTC text.
func nullTS(t *time.Time) any {
	if t == nil {
		return nil
	}
	return tsText(*t)
}

func boolInt(b bool) int64 {
	if b {
		return 1
	}
	return 0
}

func tsText(t time.Time) string { return t.UTC().Format(time.RFC3339Nano) }

func parseTS(s string) time.Time {
	if s == "" {
		return time.Time{}
	}
	t, err := time.Parse(time.RFC3339Nano, s)
	if err != nil {
		return time.Time{}
	}
	return t
}
