package index

import (
	"database/sql"
	"fmt"
	"os"
)

// ensureIntegrity runs the disposable-index guard (CLAUDE.md invariant 1). It
// opens the file, runs PRAGMA integrity_check, and — on ANY sign of corruption
// (the pragma erroring, or returning something other than "ok") — deletes the
// database and its WAL/SHM sidecars so Open recreates it empty. Returns
// rebuilt=true in that case. A non-existent or clean database returns false with
// no side effects. The rebuild is flagged upstream (Store.Rebuilt) so the caller
// knows a full rescan is required to repopulate the projection from the source
// of truth (the filesystem).
func ensureIntegrity(path string) (rebuilt bool, err error) {
	if _, statErr := os.Stat(path); os.IsNotExist(statErr) {
		return false, nil // fresh store; nothing to check
	}

	if ok := probeIntegrity(path); ok && !schemaOutdated(path) {
		return false, nil
	}

	// Corrupt/unreadable OR an outdated schema version (a breaking column change
	// like W8-E's media_type -> file_category/file_group has no in-place
	// migration): delete file + sidecars and report a rebuild. The empty path is
	// recreated with the current schema by Open and a fresh walk re-classifies
	// every item against the live taxonomy.
	for _, suffix := range []string{"", "-wal", "-shm", "-journal"} {
		if rmErr := os.Remove(path + suffix); rmErr != nil && !os.IsNotExist(rmErr) {
			return false, fmt.Errorf("remove corrupt index %q: %w", path+suffix, rmErr)
		}
	}
	return true, nil
}

// schemaOutdated reports whether an existing database was stamped by an OLDER
// schema version than the current binary (0 < user_version < schemaVersion). A
// fresh stamp (== schemaVersion) or an unreadable/unstamped file (0, or a probe
// error) is NOT treated as outdated here — corruption is already caught by
// probeIntegrity, and a 0 stamp is a fresh/legacy DB the caller recreates. This
// is what makes a breaking schema bump (W8-E) rebuild-friendly with no
// data-migration machinery.
func schemaOutdated(path string) bool {
	db, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		return false
	}
	defer db.Close()
	var v int
	if err := db.QueryRow(`PRAGMA user_version`).Scan(&v); err != nil {
		return false
	}
	return v > 0 && v < schemaVersion
}

// probeIntegrity opens a short-lived connection and returns true only when
// PRAGMA integrity_check reports exactly "ok". Any open/query error or a
// non-"ok" result is treated as corruption (returns false).
func probeIntegrity(path string) bool {
	db, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		return false
	}
	defer db.Close()

	var result string
	if err := db.QueryRow(`PRAGMA integrity_check`).Scan(&result); err != nil {
		return false
	}
	return result == "ok"
}
