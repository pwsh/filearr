package scan

import (
	"context"
	"database/sql"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
	"github.com/filearr/filearr/agent/internal/shares"
)

// ShareResolver maps a local absolute path to a best-effort network-share Hint
// (P10-T11). It is satisfied by *shares.Resolver; the scan takes the interface so
// share discovery stays optional (a nil resolver = no hints) and injectable in
// tests. Hint MUST be best-effort: nil for an uncovered path, never an error.
type ShareResolver interface {
	Hint(absPath string) *shares.Hint
}

// emit writes one replication event into tx, alongside the item mutation that
// produced it (same transaction => atomic: a rolled-back scan batch leaves
// neither). libraryRef is the scan root's absolute path verbatim — the central
// side materializes one library per (agent, libraryRef), so a multi-root scan
// (scan.json lists several) yields several libraries, each keyed on its own root
// path. fromRel is set only for a moved event (the pre-move rel_path). hint is the
// P10-T11 share hint, attached only for created/modified (nil for moved/deleted).
//
// Events are emitted from the scan call sites, NOT from index.InsertItem /
// UpdateItem / DeleteItem, because move detection drives those helpers through a
// sentinel-parking dance (a U+FFFF placeholder rel_path, a duplicate-row delete)
// whose intermediate states must never reach the wire. Emitting here keeps a
// rename to exactly one moved event and never leaks a sentinel path.
func emit(ctx context.Context, tx *sql.Tx, libraryRef, op string, it *index.Item, fromRel string, hint *outbox.ShareHint) error {
	_, err := outbox.Write(ctx, tx, outbox.Event{
		ItemID:      it.ID,
		Op:          op,
		LibraryRef:  libraryRef,
		RelPath:     it.RelPath,
		FromRelPath: fromRel,
		Size:        it.Size,
		MtimeNs:     it.MtimeNs,
		QuickHash:   it.QuickHash,
		ContentHash: it.ContentHash,
		ShareHint:   hint,
	})
	return err
}

// shareHint resolves absPath to the wire share-hint object via sr, or nil when sr
// is nil or discovery does not cover the path (the normal best-effort case, R1).
func shareHint(sr ShareResolver, absPath string) *outbox.ShareHint {
	if sr == nil {
		return nil
	}
	h := sr.Hint(absPath)
	if h == nil {
		return nil
	}
	return &outbox.ShareHint{
		ShareURL:  h.ShareURL,
		UNC:       h.UNC,
		ShareName: h.ShareName,
		Host:      h.Host,
		Source:    h.Source,
	}
}
