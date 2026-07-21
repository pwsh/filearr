//go:build !windows && !linux && !darwin

package permissions

import "io/fs"

// collectRecord is the INERT unsupported-OS seam. No ACL model is wired for
// platforms outside {windows, linux, darwin}; the collector stays inert here.
//
// TODO(W7): if a new target OS is ever supported, add its ACL read in a
// build-tagged sibling file (permissions_<os>.go) mirroring the windows/linux/
// darwin seams. NO OS I/O is issued here.
func collectRecord(path string, info fs.FileInfo) (*Record, error) {
	_, _ = path, info
	return nil, ErrPermissionsScaffold
}
