//go:build windows

package inventory

import (
	"io/fs"
	"syscall"
	"time"
)

// statTimes on Windows: both creation and access times come free from the
// Win32FileAttributeData the directory walk's stat already returned.
func statTimes(info fs.FileInfo) fileTimes {
	d, ok := info.Sys().(*syscall.Win32FileAttributeData)
	if !ok {
		return fileTimes{}
	}
	return fileTimes{
		atimeNs: filetimeNano(d.LastAccessTime),
		hasA:    true,
		btimeNs: filetimeNano(d.CreationTime),
		hasB:    true,
	}
}

// filetimeNano converts a Windows FILETIME to Unix nanoseconds.
func filetimeNano(ft syscall.Filetime) int64 {
	return time.Unix(0, ft.Nanoseconds()).UnixNano()
}
