//go:build linux

package inventory

import (
	"io/fs"
	"syscall"
)

// statTimes on Linux: access time from Stat_t.Atim. There is no cheap birth time
// in the classic stat struct (statx(2) is required and is a separate syscall), so
// btime is omitted — deliberately not paying an extra syscall in the default pass.
func statTimes(info fs.FileInfo) fileTimes {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fileTimes{}
	}
	return fileTimes{atimeNs: st.Atim.Nano(), hasA: true}
}
