package index

import (
	"context"
	"database/sql"
)

// setFlag upserts a store_flags key/value (durable across restarts). Used by Open
// to record the rebuilt marker; kept package-private since only Open writes flags.
func setFlag(db *sql.DB, key string, value int) error {
	_, err := db.Exec(
		`INSERT INTO store_flags(key, value) VALUES(?, ?)
		 ON CONFLICT(key) DO UPDATE SET value = excluded.value`,
		key, value)
	return err
}

// RebuiltPending reports whether the durable rebuilt marker is set — i.e. the
// local database was fresh-created or corruption-rebuilt and has NOT yet been
// reconciled with central. Unlike Store.Rebuilt (an in-memory field lost on
// process exit), this survives the scan→reconcile process boundary. The reconcile
// sweep treats it as a rebuilt signal (send rebuilt=true) and clears it only after
// a successful rebuilt-carrying sweep.
func (s *Store) RebuiltPending(ctx context.Context) (bool, error) {
	var v int
	err := s.db.QueryRowContext(ctx,
		`SELECT value FROM store_flags WHERE key = ?`, flagRebuiltPending).Scan(&v)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return v != 0, nil
}

// ClearRebuiltPending removes the durable rebuilt marker. The reconcile sweep
// calls this ONLY after a successful sweep that carried rebuilt=true, so a failed
// sweep leaves the marker in place for the next attempt.
func (s *Store) ClearRebuiltPending(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx,
		`DELETE FROM store_flags WHERE key = ?`, flagRebuiltPending)
	return err
}

// RootRef identifies one scan root: its local id and absolute path. The path IS
// the replication/reconcile library_ref (agentsync R1 — central stores it
// verbatim as the auto-provisioned Library.root_path).
type RootRef struct {
	ID   string
	Path string
}

// Roots returns every scan root in the local catalog, ordered by path for a
// stable per-root sweep order. Used by the P5-T5 reconcile sweep, which builds
// one full manifest per root.
func (s *Store) Roots(ctx context.Context) ([]RootRef, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id, path FROM roots ORDER BY path`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RootRef
	for rows.Next() {
		var r RootRef
		if err := rows.Scan(&r.ID, &r.Path); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// CountActive returns the total number of status='active' items across all roots.
// Used by the reconcile sweep to qualify the "outbox empty but items exist"
// rebuilt fallback (an empty outbox on a never-scanned agent is not a rebuild).
func (s *Store) CountActive(ctx context.Context) (int, error) {
	var n int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM items WHERE status = ?`, StatusActive).Scan(&n)
	return n, err
}

// ActiveItems returns every status='active' item for a root, ordered by rel_path.
// Sidecars ARE included: they replicate as plain items (agentsync apply_batch),
// so central's manifest of this root contains them too — excluding them here
// would make every sweep spuriously diverge. Missing/trashed rows are excluded
// (a tombstone is "gone"; the manifest is the set of things that exist).
func (s *Store) ActiveItems(ctx context.Context, rootID string) ([]*Item, error) {
	rows, err := s.db.QueryContext(ctx,
		selectItemColumns+` WHERE root_id = ? AND status = ? ORDER BY rel_path`,
		rootID, StatusActive)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*Item
	for rows.Next() {
		it, err := scanItem(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, it)
	}
	return out, rows.Err()
}
