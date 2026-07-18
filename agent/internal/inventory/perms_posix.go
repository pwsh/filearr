//go:build !windows

package inventory

import (
	"context"
	"fmt"
	"io/fs"
	"strconv"
	"strings"
	"syscall"

	"golang.org/x/sys/unix"
)

// permsCollector (POSIX) reports the octal permission bits (from the walk's stat,
// free) plus the LIST of extended-attribute NAMES on the entry. The xattr listing
// is a SEPARATE syscall per file (Llistxattr — l-variant so a symlink is not
// followed), so this collector is opt-in territory (W6-R1 §6); names only, never
// values (POSIX ACLs, macOS quarantine/tags surface here as names).
type permsCollector struct{}

func (permsCollector) Name() string { return "perms" }

func (permsCollector) Collect(_ context.Context, path string, info fs.FileInfo) (map[string]any, error) {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return nil, fmt.Errorf("perms: no POSIX stat for entry")
	}
	// Permission + setuid/setgid/sticky bits (low 12 bits), as a 4-digit octal.
	perm := st.Mode & 0o7777
	m := map[string]any{
		"mode_octal": "0" + strconv.FormatUint(uint64(perm), 8),
	}
	if names, err := listXattr(path); err == nil && len(names) > 0 {
		m["xattrs"] = names
	}
	return m, nil
}

// listXattr returns the extended-attribute names on path (symlink not followed).
// A two-phase size query avoids a fixed buffer; ERANGE between the two (a racing
// xattr add) yields a best-effort empty list rather than an error into the walk.
func listXattr(path string) ([]string, error) {
	sz, err := unix.Llistxattr(path, nil)
	if err != nil || sz == 0 {
		return nil, err
	}
	buf := make([]byte, sz)
	n, err := unix.Llistxattr(path, buf)
	if err != nil {
		return nil, err
	}
	var names []string
	for _, raw := range strings.Split(string(buf[:n]), "\x00") {
		if raw != "" {
			names = append(names, raw)
		}
	}
	return names, nil
}
