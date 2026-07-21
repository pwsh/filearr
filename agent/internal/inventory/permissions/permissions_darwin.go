//go:build darwin

package permissions

import "io/fs"

// readACL is the INERT macOS ACL read seam.
//
// TODO(W7-T4): implement by exec'ing `ls -le` PER DIRECTORY with LC_ALL=C forced
// (the fleet builds CGO_ENABLED=0, which blocks the native acl_get_file /
// ACL_TYPE_EXTENDED libSystem call, §9) and parsing the NFSv4-style ORDERED
// allow/deny ACE text (preserve OrderIndex; map masks via NFSv4MaskToVerbs). Also
// surface BSD chflags (uchg/schg/uappnd/sappnd) as a SEPARATE whole-object field,
// never folded into the ACE verb list (§1.3), via the W6-R1-flagged st_flags
// shim. macOS reads inherit the same TCC/Full-Disk-Access gating as content
// listing — a protected-root failure must be the "FDA suspected" health state,
// not a silent empty ACE list. NO real exec is issued here yet.
func readACL(path string, info fs.FileInfo) (*Record, error) {
	_, _ = path, info
	return nil, ErrPermissionsScaffold
}

// collectRecord is the uniform per-OS entry point Collect routes through.
func collectRecord(path string, info fs.FileInfo) (*Record, error) {
	return readACL(path, info)
}
