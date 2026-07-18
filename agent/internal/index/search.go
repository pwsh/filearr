package index

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
)

// OpenReadOnly opens a dedicated SQLITE_OPEN_READONLY-equivalent connection to an
// existing index at path, strictly separate from the writable Store (P7-T1
// defense in depth, brief §3.4: the local query surface is always read-only, so
// no parsed DSL input can ever coerce a write on this handle). modernc/sqlite
// enforces this via the URI `mode=ro` (rejects writes with "attempt to write a
// readonly database") plus `query_only(true)` as a second, statement-level bar;
// the connection still observes the writer's committed WAL frames. The caller
// owns Close.
func OpenReadOnly(path string) (*sql.DB, error) {
	dsn := "file:" + filepath.ToSlash(path) +
		"?mode=ro&_pragma=query_only(true)&_pragma=busy_timeout(5000)&_pragma=foreign_keys(ON)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite read-only: %w", err)
	}
	db.SetMaxOpenConns(1)
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("ping sqlite read-only: %w", err)
	}
	return db, nil
}

// SelectItemColumnsQualified is the table-qualified item projection (SELECT ...
// FROM items) that an FTS-joined read can extend with JOIN/WHERE/ORDER. Exposed
// (additive) so the query package assembles its parameterised SQL while reusing
// the canonical column list + ScanItem.
const SelectItemColumnsQualified = selectItemColumnsQualified

// RowScanner is satisfied by *sql.Row and *sql.Rows.
type RowScanner interface {
	Scan(dest ...any) error
}

// ScanItem materialises one Item from a query row using the canonical column
// order of SelectItemColumnsQualified. Exported (additive) for the query package.
func ScanItem(r RowScanner) (*Item, error) {
	return scanItem(r)
}

// Search runs an FTS5 trigram MATCH over filename+rel_path and returns the
// matching items ordered by rank. When includeSidecars is false, is_sidecar rows
// are excluded (mirrors central hiding sidecars from default search; P7 will own
// the real query surface). limit<=0 applies a default cap.
//
// The query is passed to FTS5 as a bare-term MATCH. FTS5 syntax characters in a
// user query would otherwise raise a "malformed MATCH" error, so the query is
// wrapped as a single quoted string token, which the trigram tokenizer treats as
// a substring probe — the intended behaviour for a provisional CLI.
func (s *Store) Search(ctx context.Context, query string, includeSidecars bool, limit int) ([]Item, error) {
	if limit <= 0 {
		limit = 50
	}
	q := strings.TrimSpace(query)
	if q == "" {
		return nil, nil
	}
	match := `"` + strings.ReplaceAll(q, `"`, `""`) + `"`

	sqlStr := selectItemColumnsQualified + `
JOIN items_fts ON items_fts.rowid = items.rowid
WHERE items_fts MATCH ?`
	if !includeSidecars {
		sqlStr += ` AND items.is_sidecar = 0`
	}
	sqlStr += ` ORDER BY rank LIMIT ?`

	rows, err := s.db.QueryContext(ctx, sqlStr, match, limit)
	if err != nil {
		return nil, fmt.Errorf("fts search: %w", err)
	}
	defer rows.Close()

	var out []Item
	for rows.Next() {
		it, err := scanItem(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *it)
	}
	return out, rows.Err()
}

// selectItemColumnsQualified mirrors selectItemColumns with table-qualified
// columns for the FTS join (bare column names would be ambiguous against
// items_fts). Kept column-aligned with scanItem / selectItemColumns.
const selectItemColumnsQualified = `
SELECT items.id, items.root_id, items.rel_path, items.filename, items.extension,
       items.size, items.mtime_ns, items.quick_hash, items.content_hash,
       items.media_type, items.meta, items.status, items.is_sidecar,
       items.sidecar_of, items.first_seen, items.last_seen, items.synced_at,
       items.local_seq_no
FROM items`
