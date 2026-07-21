// Package index is the agent's offline-first local catalog: a SQLite store with
// an FTS5 trigram projection over filename+rel_path, mirroring a narrow subset of
// central's items table (agent/docs/layout.md §internal/index). The search index
// is disposable (CLAUDE.md invariant 1): IntegrityGuard rebuilds it from a fresh
// walk on corruption. Every mutation takes a *sql.Tx so a future outbox insert
// (P5-T4) can join the same transaction (plan ruling 3).
package index

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"time"

	"github.com/google/uuid"
	_ "modernc.org/sqlite" // pure-Go driver, FTS5 + trigram built in (CGO-free)
)

// flagRebuiltPending is the store_flags key that durably records that the local
// database was fresh-created or corruption-rebuilt (P5-T5). See schema.go.
const flagRebuiltPending = "rebuilt_pending"

// Item status values mirror backend/filearr/models.py:ItemStatus. Scans write
// only active↔missing; trashed is never scan-written (reserved for parity).
const (
	StatusActive  = "active"
	StatusMissing = "missing"
	StatusTrashed = "trashed"
)

// Item is one row of the local catalog. Empty-string hash/extension/sidecar_of
// fields are persisted as SQL NULL (and read back as ""); a NULL/"" QuickHash is
// the self-heal signal (re-queue hashing) exactly as in central. SyncedAt is a
// pointer so "never synced" is a genuine NULL for P5-T4's replication cursor.
type Item struct {
	ID          string
	RootID      string
	RelPath     string
	Filename    string
	Extension   string
	Size        int64
	MtimeNs     int64
	QuickHash   string
	ContentHash string
	// FileCategory / FileGroup are the File Extension Similarity Taxonomy pair
	// (W8-E), replacing the old static MediaType. Written from the taxonomy cache
	// snapshot at scan time so operator taxonomy edits take effect locally. Central
	// re-classifies authoritatively on apply, so these are purely local signal for
	// the query surface + thumbnail gating.
	FileCategory string
	FileGroup    string
	Meta         string // JSON; "" => NULL
	Status       string
	IsSidecar    bool
	SidecarOf    string
	FirstSeen    time.Time
	LastSeen     time.Time
	SyncedAt     *time.Time
	LocalSeqNo   int64
}

// Store owns the SQLite connection. Rebuilt is true when IntegrityGuard had to
// delete and recreate a corrupt database on Open, so the caller (and, later,
// central) can observe that a full rescan is required.
type Store struct {
	db      *sql.DB
	path    string
	Rebuilt bool
}

// Open opens (or creates) the store at path. It first runs the IntegrityGuard:
// a corrupt file is deleted and recreated empty (Rebuilt=true), then the schema
// is (re)applied and WAL mode enabled. A brand-new or clean database opens with
// Rebuilt=false.
func Open(path string) (*Store, error) {
	// Fresh-create detection BEFORE ensureIntegrity may create/delete the file: a
	// missing file means a brand-new store, which — like a corruption rebuild —
	// resets the local seq base and so must durably flag rebuilt_pending.
	_, statErr := os.Stat(path)
	freshCreate := os.IsNotExist(statErr)

	rebuilt, err := ensureIntegrity(path)
	if err != nil {
		return nil, err
	}
	db, err := openDB(path)
	if err != nil {
		return nil, err
	}
	if err := migrate(db); err != nil {
		db.Close()
		return nil, err
	}
	if rebuilt || freshCreate {
		if err := setFlag(db, flagRebuiltPending, 1); err != nil {
			db.Close()
			return nil, fmt.Errorf("record rebuilt marker: %w", err)
		}
	}
	return &Store{db: db, path: path, Rebuilt: rebuilt}, nil
}

// openDB opens the driver with WAL, foreign keys, and a busy timeout. A single
// connection avoids modernc/sqlite writer contention for this single-process
// embedded store.
func openDB(path string) (*sql.DB, error) {
	dsn := path + "?_pragma=journal_mode(WAL)&_pragma=foreign_keys(ON)&_pragma=busy_timeout(5000)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	db.SetMaxOpenConns(1)
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}
	return db, nil
}

// Close closes the underlying database.
func (s *Store) Close() error { return s.db.Close() }

// DB exposes the handle for read queries and test assertions.
func (s *Store) DB() *sql.DB { return s.db }

// Begin starts a transaction. All catalog mutations join a caller-provided *Tx
// so a future outbox write lands atomically alongside the item change.
func (s *Store) Begin(ctx context.Context) (*sql.Tx, error) {
	return s.db.BeginTx(ctx, nil)
}

// EnsureRoot returns the id of the roots row for absPath, creating it if absent
// (UUIDv7). rel_path uniqueness is scoped per root via items(root_id, rel_path),
// so multiple roots (scan.json may list several) never collide.
func EnsureRoot(ctx context.Context, tx *sql.Tx, absPath string) (string, error) {
	var id string
	err := tx.QueryRowContext(ctx, `SELECT id FROM roots WHERE path = ?`, absPath).Scan(&id)
	if err == nil {
		return id, nil
	}
	if err != sql.ErrNoRows {
		return "", fmt.Errorf("lookup root: %w", err)
	}
	id, err = NewID()
	if err != nil {
		return "", err
	}
	if _, err := tx.ExecContext(ctx,
		`INSERT INTO roots(id, path, added_at) VALUES(?, ?, ?)`,
		id, absPath, nowUTC(),
	); err != nil {
		return "", fmt.Errorf("insert root: %w", err)
	}
	return id, nil
}

// LoadItems loads every item for a root, keyed by rel_path — the whole-root diff
// map (read-only context for move detection + sidecar association, mirroring
// scan.py's `existing`).
func (s *Store) LoadItems(ctx context.Context, rootID string) (map[string]*Item, error) {
	rows, err := s.db.QueryContext(ctx, selectItemColumns+` WHERE root_id = ?`, rootID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]*Item{}
	for rows.Next() {
		it, err := scanItem(rows)
		if err != nil {
			return nil, err
		}
		out[it.RelPath] = it
	}
	return out, rows.Err()
}

// InsertItem inserts a new item, assigning a fresh local sequence number within
// tx. It is the primary Tx seam for P5-T4: an outbox INSERT keyed on the same
// LocalSeqNo can be added here without changing callers.
func InsertItem(ctx context.Context, tx *sql.Tx, it *Item) error {
	seq, err := nextSeq(ctx, tx)
	if err != nil {
		return err
	}
	it.LocalSeqNo = seq
	_, err = tx.ExecContext(ctx, `
INSERT INTO items(id, root_id, rel_path, filename, extension, size, mtime_ns,
                  quick_hash, content_hash, file_category, file_group, meta, status, is_sidecar,
                  sidecar_of, first_seen, last_seen, synced_at, local_seq_no)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		it.ID, it.RootID, it.RelPath, it.Filename, nullStr(it.Extension), it.Size, it.MtimeNs,
		nullStr(it.QuickHash), nullStr(it.ContentHash), nullStr(it.FileCategory), nullStr(it.FileGroup),
		nullStr(it.Meta), it.Status,
		boolInt(it.IsSidecar), nullStr(it.SidecarOf), tsText(it.FirstSeen), tsText(it.LastSeen),
		nullTS(it.SyncedAt), it.LocalSeqNo,
	)
	if err != nil {
		return fmt.Errorf("insert item: %w", err)
	}
	return nil
}

// UpdateItem rewrites every mutable column of an existing item and stamps a fresh
// local sequence number (the row changed, so the outbox cursor must advance).
func UpdateItem(ctx context.Context, tx *sql.Tx, it *Item) error {
	seq, err := nextSeq(ctx, tx)
	if err != nil {
		return err
	}
	it.LocalSeqNo = seq
	_, err = tx.ExecContext(ctx, `
UPDATE items SET rel_path=?, filename=?, extension=?, size=?, mtime_ns=?,
                 quick_hash=?, content_hash=?, file_category=?, file_group=?, meta=?, status=?,
                 is_sidecar=?, sidecar_of=?, last_seen=?, synced_at=?, local_seq_no=?
WHERE id=?`,
		it.RelPath, it.Filename, nullStr(it.Extension), it.Size, it.MtimeNs,
		nullStr(it.QuickHash), nullStr(it.ContentHash), nullStr(it.FileCategory), nullStr(it.FileGroup),
		nullStr(it.Meta), it.Status,
		boolInt(it.IsSidecar), nullStr(it.SidecarOf), tsText(it.LastSeen), nullTS(it.SyncedAt),
		it.LocalSeqNo, it.ID,
	)
	if err != nil {
		return fmt.Errorf("update item: %w", err)
	}
	return nil
}

// DeleteItem removes an item row (used when a move transfer drops the freshly
// inserted duplicate). The FTS delete trigger keeps the projection in sync.
func DeleteItem(ctx context.Context, tx *sql.Tx, id string) error {
	if _, err := tx.ExecContext(ctx, `DELETE FROM items WHERE id = ?`, id); err != nil {
		return fmt.Errorf("delete item: %w", err)
	}
	return nil
}

// NewID returns a UUIDv7 string — locally generated, becoming the replication
// identity per agentsync design (plan ruling 4).
func NewID() (string, error) {
	id, err := uuid.NewV7()
	if err != nil {
		return "", fmt.Errorf("generate uuidv7: %w", err)
	}
	return id.String(), nil
}

// nextSeq allocates the next monotonic local sequence number inside tx. This is
// the ordering key a P5-T4 drain will read (WHERE synced_at IS NULL ORDER BY
// local_seq_no); allocating it in the same tx as the item write keeps them
// atomic.
func nextSeq(ctx context.Context, tx *sql.Tx) (int64, error) {
	var seq int64
	err := tx.QueryRowContext(ctx,
		`UPDATE local_meta SET next_seq = next_seq + 1 WHERE id = 1 RETURNING next_seq`,
	).Scan(&seq)
	if err != nil {
		return 0, fmt.Errorf("allocate local_seq_no: %w", err)
	}
	return seq, nil
}

func nowUTC() string { return time.Now().UTC().Format(time.RFC3339Nano) }
