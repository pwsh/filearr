// Package history is the agent's LOCAL-ONLY query frecency store (P7-T6): a
// zoxide-style frequency+recency ranking of the DSL queries a same-machine user
// has run, so repeated searches can be surfaced as suggestions over time.
//
// # Architectural isolation (the load-bearing invariant)
//
// Search terms are a sensitive local signal and MUST NEVER leave the machine
// (research §6). This package guarantees that by construction, not by policy:
//
//   - History lives in its OWN SQLite database file (history.db), physically
//     separate from the index/outbox database (index.db). The replication /
//     outbox subsystem (internal/outbox) is only ever handed the index Store's
//     *sql.DB handle; it never opens this file and holds no handle to it, so
//     there is no code path — not even an accidental SELECT — from replication
//     to a history row. A shared *sql.Tx between a history write and an
//     item/outbox mutation is impossible: they are different databases.
//   - This is strictly stronger than a same-DB "history table the outbox never
//     names" (which would rely on the outbox code's discipline). Here the outbox
//     writer is INCAPABLE of touching history, which is exactly the P7-T6 bar
//     ("incapable of leaving the machine, not merely policy-gated").
//
// The store contains zero networking code; nothing here can transmit a row.
package history

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"sort"
	"strings"
	"time"

	_ "modernc.org/sqlite" // pure-Go driver (CGO-free), same as internal/index
)

// schemaVersion is stamped into PRAGMA user_version for a future migration.
const schemaVersion = 1

// Frecency + maintenance constants. The scoring mirrors zoxide's recency-bucket
// model (research §6): a stored frequency (rank) multiplied at read time by a
// recency weight, so a query that is both frequent AND recent ranks highest.
const (
	// Recency-bucket boundaries (seconds) and their multipliers — zoxide's shape.
	bucketHour = int64(3600)
	bucketDay  = int64(86400)
	bucketWeek = int64(604800)

	multHour  = 4.0
	multDay   = 2.0
	multWeek  = 0.5
	multOlder = 0.25

	// defaultMaxTotal is the ceiling on the summed rank of all rows. When the sum
	// exceeds it, a decay pass halves every rank (the "halve scores periodically"
	// shape). Matches zoxide's _ZO_MAXAGE default order of magnitude.
	defaultMaxTotal = 10000.0
	// decayFactor is applied to every rank during a decay pass.
	decayFactor = 0.5
	// pruneEpsilon: after a decay pass, rows whose rank falls below this floor are
	// deleted (a query used once, long ago, decays out rather than lingering).
	pruneEpsilon = 1.0
	// retentionSeconds prunes entries untouched for this long (zoxide prunes stale
	// entries after 90 days) regardless of the decay ceiling.
	retentionSeconds = int64(90 * 24 * 60 * 60)
)

// Entry is one ranked history row. Hits is the accumulated frequency (rank);
// Score is the frecency = Hits × recency-bucket weight computed at read time.
type Entry struct {
	Query    string
	Hits     float64
	LastUsed time.Time
	Score    float64
}

// Store owns the dedicated history database connection. It is opened read-write
// (recording needs to write), but it is a SEPARATE file+handle from the index —
// see the package doc for why that is the isolation guarantee. Construct with
// Open; call Close when done.
type Store struct {
	db  *sql.DB
	now func() time.Time
	// maxTotal is the decay ceiling (defaultMaxTotal); a field so tests can force
	// a decay pass without recording thousands of rows.
	maxTotal float64
}

// Open opens (or creates) the history store at path (a dedicated file, NOT the
// index database). WAL + a busy timeout match the index store's single-process
// discipline.
func Open(path string) (*Store, error) {
	dsn := "file:" + filepath.ToSlash(path) +
		"?_pragma=journal_mode(WAL)&_pragma=busy_timeout(5000)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open history db: %w", err)
	}
	db.SetMaxOpenConns(1)
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("ping history db: %w", err)
	}
	if err := migrate(db); err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db: db, now: time.Now, maxTotal: defaultMaxTotal}, nil
}

func migrate(db *sql.DB) error {
	const schemaSQL = `
CREATE TABLE IF NOT EXISTS query_history (
    query     TEXT PRIMARY KEY,   -- normalized DSL query text
    rank      REAL NOT NULL,      -- accumulated frequency (zoxide-style)
    last_used INTEGER NOT NULL    -- unix seconds of the most recent use
);
CREATE INDEX IF NOT EXISTS ix_query_history_last_used ON query_history(last_used);
`
	if _, err := db.Exec(schemaSQL); err != nil {
		return fmt.Errorf("apply history schema: %w", err)
	}
	if _, err := db.Exec(fmt.Sprintf("PRAGMA user_version = %d", schemaVersion)); err != nil {
		return fmt.Errorf("stamp history schema version: %w", err)
	}
	return nil
}

// Close releases the connection.
func (s *Store) Close() error { return s.db.Close() }

// Normalize canonicalizes a raw query for frecency grouping: it trims and
// collapses internal whitespace runs to single spaces. Case is preserved — DSL
// values (e.g. path: globs) can be case-significant, and the stored text should
// stay faithful to what the user typed. Returns "" for a blank/whitespace query
// (which Record skips).
func Normalize(raw string) string {
	return strings.Join(strings.Fields(raw), " ")
}

// Record bumps the frecency of a successfully-run query: it normalizes raw,
// increments its rank (+1 per use), stamps last_used=now, then runs an
// opportunistic maintenance pass (decay when the rank sum exceeds the ceiling,
// plus a floor/retention prune). A blank query is ignored. There is no daemon
// timer — maintenance rides the write path, so an idle agent does no work.
func (s *Store) Record(ctx context.Context, raw string) error {
	q := Normalize(raw)
	if q == "" {
		return nil
	}
	now := s.now().Unix()

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("history: begin: %w", err)
	}
	defer func() { _ = tx.Rollback() }()

	if _, err := tx.ExecContext(ctx,
		`INSERT INTO query_history(query, rank, last_used)
		 VALUES(?, 1.0, ?)
		 ON CONFLICT(query) DO UPDATE SET rank = rank + 1.0, last_used = excluded.last_used`,
		q, now,
	); err != nil {
		return fmt.Errorf("history: upsert: %w", err)
	}
	if err := maintain(ctx, tx, now, s.maxTotal); err != nil {
		return err
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("history: commit: %w", err)
	}
	return nil
}

// maintain runs the opportunistic decay + prune inside tx:
//   - retention prune: drop rows untouched past retentionSeconds.
//   - decay: when SUM(rank) exceeds maxTotal, halve every rank, then drop rows
//     whose rank fell below the epsilon floor.
func maintain(ctx context.Context, tx *sql.Tx, now int64, maxTotal float64) error {
	if _, err := tx.ExecContext(ctx,
		`DELETE FROM query_history WHERE last_used < ?`, now-retentionSeconds,
	); err != nil {
		return fmt.Errorf("history: retention prune: %w", err)
	}

	var total sql.NullFloat64
	if err := tx.QueryRowContext(ctx, `SELECT SUM(rank) FROM query_history`).Scan(&total); err != nil {
		return fmt.Errorf("history: sum rank: %w", err)
	}
	if !total.Valid || total.Float64 <= maxTotal {
		return nil
	}
	if _, err := tx.ExecContext(ctx,
		`UPDATE query_history SET rank = rank * ?`, decayFactor,
	); err != nil {
		return fmt.Errorf("history: decay: %w", err)
	}
	if _, err := tx.ExecContext(ctx,
		`DELETE FROM query_history WHERE rank < ?`, pruneEpsilon,
	); err != nil {
		return fmt.Errorf("history: floor prune: %w", err)
	}
	return nil
}

// Top returns up to limit history entries ranked by frecency (highest first),
// ties broken by query text for a stable order. It is a pure read — the ranking
// surface the socket API exposes for suggestions.
func (s *Store) Top(ctx context.Context, limit int) ([]Entry, error) {
	if limit <= 0 {
		limit = 20
	}
	rows, err := s.db.QueryContext(ctx,
		`SELECT query, rank, last_used FROM query_history`)
	if err != nil {
		return nil, fmt.Errorf("history: select: %w", err)
	}
	defer rows.Close()

	now := s.now().Unix()
	var out []Entry
	for rows.Next() {
		var (
			e        Entry
			lastUsed int64
		)
		if err := rows.Scan(&e.Query, &e.Hits, &lastUsed); err != nil {
			return nil, err
		}
		e.LastUsed = time.Unix(lastUsed, 0).UTC()
		e.Score = e.Hits * recencyWeight(now-lastUsed)
		out = append(out, e)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Score != out[j].Score {
			return out[i].Score > out[j].Score
		}
		return out[i].Query < out[j].Query
	})
	if len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}

// recencyWeight maps an age (seconds) to its zoxide-style recency multiplier.
func recencyWeight(ageSeconds int64) float64 {
	switch {
	case ageSeconds < bucketHour:
		return multHour
	case ageSeconds < bucketDay:
		return multDay
	case ageSeconds < bucketWeek:
		return multWeek
	default:
		return multOlder
	}
}
