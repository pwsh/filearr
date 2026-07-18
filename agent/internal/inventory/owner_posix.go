//go:build !windows

package inventory

import (
	"context"
	"fmt"
	"io/fs"
	"os/user"
	"strconv"
	"syscall"
)

// ownerCollector (POSIX) reports the uid/gid from the stat the walk already did
// (zero extra syscalls) plus their resolved names via the local user database.
// Name resolution can miss (an id with no passwd entry, e.g. a container-mapped
// uid) — that is reported by omission, never an error.
type ownerCollector struct{}

func (ownerCollector) Name() string { return "owner" }

func (ownerCollector) Collect(_ context.Context, _ string, info fs.FileInfo) (map[string]any, error) {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return nil, fmt.Errorf("owner: no POSIX stat for entry")
	}
	m := map[string]any{
		"uid": int64(st.Uid),
		"gid": int64(st.Gid),
	}
	if u, err := user.LookupId(strconv.FormatUint(uint64(st.Uid), 10)); err == nil {
		m["user"] = u.Username
	}
	if g, err := user.LookupGroupId(strconv.FormatUint(uint64(st.Gid), 10)); err == nil {
		m["group"] = g.Name
	}
	return m, nil
}
