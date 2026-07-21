package index

import (
	"database/sql"
	"fmt"
)

// schemaVersion is stamped into PRAGMA user_version so a future migration can
// detect and upgrade an older store. v2 adds the P5-T4 replication outbox; v3
// adds the store_flags table (the durable rebuilt marker, P5-T5); v4 adds the
// P12-T13 thumb_markers table (local-only thumbnail generation cursor); v5
// (W8-E) replaces the static media_type column with the File Extension
// Similarity Taxonomy pair file_category + file_group. No in-place migration —
// an older store fails integrity/version and is rebuilt from a fresh walk
// (disposable-index philosophy, invariant 1), which re-classifies every item
// against the live taxonomy.
const schemaVersion = 5

// schemaSQL is the full DDL. The items table mirrors a narrow subset of central
// items (agent/docs/layout.md): identity is (root_id, rel_path). mtime_ns is
// INTEGER Unix nanoseconds (ruling 2). The FTS5 external-content table over
// filename+rel_path uses the trigram tokenizer (ruling 1: chosen now so P7's
// trigram-MATCH query surface needs no schema rebuild). local_seq_no + synced_at
// are the local-only replication cursor columns.
const schemaSQL = `
CREATE TABLE IF NOT EXISTS roots (
    id       TEXT PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    root_id      TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    rel_path     TEXT NOT NULL,
    filename     TEXT NOT NULL,
    extension    TEXT,
    size         INTEGER,
    mtime_ns     INTEGER,
    quick_hash    TEXT,
    content_hash  TEXT,
    file_category TEXT,
    file_group    TEXT,
    meta          TEXT,
    status       TEXT NOT NULL,
    is_sidecar   INTEGER NOT NULL DEFAULT 0,
    sidecar_of   TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    synced_at    TEXT,
    local_seq_no INTEGER NOT NULL DEFAULT 0,
    UNIQUE(root_id, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_items_root_status ON items(root_id, status);
CREATE INDEX IF NOT EXISTS idx_items_quick       ON items(root_id, quick_hash, size);
CREATE INDEX IF NOT EXISTS idx_items_unsynced    ON items(synced_at, local_seq_no);

CREATE TABLE IF NOT EXISTS local_meta (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL
);
INSERT OR IGNORE INTO local_meta(id, next_seq) VALUES(1, 0);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    filename, rel_path,
    content='items', content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, filename, rel_path)
    VALUES (new.rowid, new.filename, new.rel_path);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, filename, rel_path)
    VALUES ('delete', old.rowid, old.filename, old.rel_path);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, filename, rel_path)
    VALUES ('delete', old.rowid, old.filename, old.rel_path);
    INSERT INTO items_fts(rowid, filename, rel_path)
    VALUES (new.rowid, new.filename, new.rel_path);
END;

-- P5-T4 transactional outbox. Each row is one AgentEvent (backend
-- filearr/agentsync.py wire contract) written IN THE SAME *sql.Tx as the item
-- mutation that produced it, so a rolled-back scan batch leaves neither the item
-- change nor its event. seq_no is AUTOINCREMENT (never reused, even after a row
-- is marked sent) and IS the durable wire seq_no the central seq-gap guard keys
-- on — the items.local_seq_no column stays a purely local bookkeeping cursor and
-- no longer feeds the wire (agent/internal/outbox docs the unification). payload
-- is the AgentEvent JSON minus seq_no; the drain injects seq_no from this column.
CREATE TABLE IF NOT EXISTS outbox (
    seq_no     INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL,
    op         TEXT NOT NULL,
    payload    TEXT NOT NULL,
    written_at TEXT NOT NULL,
    sent_at    TEXT,
    batch_id   TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_unsent ON outbox(seq_no) WHERE sent_at IS NULL;

-- P5-T5 durable store flags. A key/value scratch table that SURVIVES a process
-- restart (unlike Store.Rebuilt, an in-memory field). Open writes
-- rebuilt_pending=1 whenever it fresh-creates OR corruption-rebuilds the
-- database, so a LATER process (e.g. a scan rebuilds, then a separate
-- reconcile run) still knows the local seq base was reset and must send
-- rebuilt=true so central resets its per-agent watermark -- otherwise fresh low
-- seq_no rows are silently fast-forwarded away as stale (no apply). The
-- reconcile sweep clears it only after a successful rebuilt-carrying sweep.
CREATE TABLE IF NOT EXISTS store_flags (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

-- P12-T13 thumbnail generation cursor. Records which (item, tier) thumbnails the
-- agent has already generated + uploaded to central, keyed on the content-address
-- cache_key. This is a PURELY LOCAL table: it is NEVER replicated (no outbox row,
-- not in the AgentEvent payload) — central owns the authoritative thumbnail_manifest.
-- A stored cache_key that differs from the freshly-computed expected key (a changed
-- file → new hash → new key, or a GeneratorVersion bump) means "regenerate". The FK
-- CASCADE drops markers when their item is deleted so the table self-cleans.
CREATE TABLE IF NOT EXISTS thumb_markers (
    item_id     TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tier        INTEGER NOT NULL,
    cache_key   TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    PRIMARY KEY (item_id, tier)
);
`

// migrate applies the schema idempotently and stamps the version.
func migrate(db *sql.DB) error {
	if _, err := db.Exec(schemaSQL); err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	if _, err := db.Exec(fmt.Sprintf("PRAGMA user_version = %d", schemaVersion)); err != nil {
		return fmt.Errorf("stamp schema version: %w", err)
	}
	return nil
}
