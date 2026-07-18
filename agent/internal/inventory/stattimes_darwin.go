//go:build darwin

package inventory

import (
	"io/fs"
	"syscall"
)

// statTimes on macOS: access time from Atimespec and birth time from
// Birthtimespec — both present in Darwin's stat struct, so both are free.
func statTimes(info fs.FileInfo) fileTimes {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return fileTimes{}
	}
	return fileTimes{
		atimeNs: st.Atimespec.Nano(),
		hasA:    true,
		btimeNs: st.Birthtimespec.Nano(),
		hasB:    true,
	}
}
