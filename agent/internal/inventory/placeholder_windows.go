//go:build windows

package inventory

import (
	"context"
	"io/fs"
	"syscall"
)

// Cloud-file placeholder attributes (W6-R1 §1.2). A file carrying either bit is a
// Files-On-Demand / cloud placeholder whose content is NOT local; a size/attribute
// inventory reads these attributes safely (GetFileAttributesEx-class, which the
// walk already did) but MUST NOT open content, or it silently hydrates the file.
// x/sys/windows does not name these, so they are transcribed from the Win32 spec.
const (
	fileAttributeRecallOnDataAccess = 0x00400000
	fileAttributeRecallOnOpen       = 0x00040000
	fileAttributeOffline            = 0x00001000
)

// placeholderCollector reports whether the entry is a cloud placeholder and, if
// so, which signal fired — WITHOUT ever opening content. On Windows the signal is
// the recall/offline attribute set the walk's stat already returned.
type placeholderCollector struct{}

func (placeholderCollector) Name() string { return "placeholder" }

func (placeholderCollector) Collect(_ context.Context, _ string, info fs.FileInfo) (map[string]any, error) {
	attrs := winAttributes(info)
	recall := attrs&(fileAttributeRecallOnDataAccess|fileAttributeRecallOnOpen) != 0
	offline := attrs&fileAttributeOffline != 0
	m := map[string]any{"placeholder": recall}
	if recall || offline {
		m["cloud_only"] = true
		m["offline"] = offline
	}
	return m, nil
}

func winAttributes(info fs.FileInfo) uint32 {
	if d, ok := info.Sys().(*syscall.Win32FileAttributeData); ok {
		return d.FileAttributes
	}
	return 0
}

// isPlaceholder reports whether the entry is a cloud placeholder (content not
// hydrated) — the runner's cheap per-entry check for the placeholders_skipped
// summary counter, independent of whether the `placeholder` collector is enabled.
func isPlaceholder(info fs.FileInfo) bool {
	return winAttributes(info)&(fileAttributeRecallOnDataAccess|fileAttributeRecallOnOpen) != 0
}
