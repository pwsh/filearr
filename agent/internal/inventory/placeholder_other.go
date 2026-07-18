//go:build !windows

package inventory

import (
	"context"
	"io/fs"
)

// placeholderCollector (non-Windows) is best-effort/absent per W6-R1: macOS
// iCloud "dataless" detection needs a raw getattrlist/SF_DATALESS read that is not
// available from a stable pure-Go stdlib call (§3.3 flags it as its own spike), so
// v1 reports placeholder=false rather than guessing. The collector still EXISTS on
// every OS so a composition referencing it never fails as "unknown collector".
type placeholderCollector struct{}

func (placeholderCollector) Name() string { return "placeholder" }

func (placeholderCollector) Collect(_ context.Context, _ string, _ fs.FileInfo) (map[string]any, error) {
	return map[string]any{"placeholder": false}, nil
}

// isPlaceholder is always false off Windows (see above) — the summary's
// placeholders_skipped counter stays 0 rather than reporting a false state.
func isPlaceholder(_ fs.FileInfo) bool { return false }
