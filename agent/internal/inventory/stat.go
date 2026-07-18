package inventory

import (
	"context"
	"io/fs"
)

// statCollector reports the size/mtime/mode already paid for by the walk, plus
// the created/accessed timestamps that are cheaply available per-OS (from the
// FileInfo.Sys() the walk's stat already produced — no extra syscall). It is the
// cross-OS baseline collector.
type statCollector struct{}

func (statCollector) Name() string { return "stat" }

func (statCollector) Collect(_ context.Context, _ string, info fs.FileInfo) (map[string]any, error) {
	m := map[string]any{
		"size":     info.Size(),
		"mode":     info.Mode().String(),
		"mtime_ns": info.ModTime().UnixNano(),
		"is_dir":   info.IsDir(),
	}
	t := statTimes(info)
	if t.hasA {
		m["atime_ns"] = t.atimeNs
	}
	if t.hasB {
		m["btime_ns"] = t.btimeNs
	}
	return m, nil
}

// fileTimes carries the OS-cheap timestamps beyond mtime: access time and birth
// (creation) time where the platform exposes them from a plain stat.
type fileTimes struct {
	atimeNs int64
	hasA    bool
	btimeNs int64
	hasB    bool
}
