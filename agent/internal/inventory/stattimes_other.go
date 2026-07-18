//go:build !linux && !darwin && !windows

package inventory

import "io/fs"

// statTimes fallback for any other platform: only mtime (already in the base
// stat record) is portable, so no extra timestamps are reported.
func statTimes(info fs.FileInfo) fileTimes { return fileTimes{} }
