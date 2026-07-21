//go:build linux

package permissions

import "io/fs"

// readACL is the INERT Linux ACL read seam.
//
// TODO(W7-T2): implement via golang.org/x/sys/unix.Lgetxattr(path,
// "system.posix_acl_access", buf) and "system.posix_acl_default" (directories),
// feeding the raw bytes to DecodePosixACL → PosixACL.ToACEs (access ACL as
// ScopeThis, default ACL as ScopeDirDefault — SEPARATE lists per §9). Stamp
// Fidelity from DetectMountFidelity(/proc/mounts, path): a cifs mount without
// cifsacl is FidelitySynthesizedFromMode, not real ACL data. Gate the NFSv4
// branch on fstype == nfs4 (richacl never landed upstream, §9) and read it via
// the nfs4_getfacl text output, NOT a hand-rolled wire decoder. The xattr read
// and the /proc/mounts read are the ONLY OS boundaries; the decode/classify are
// already pure (posixacl.go/fidelity.go). NO real syscall is issued here yet.
func readACL(path string, info fs.FileInfo) (*Record, error) {
	_, _ = path, info
	return nil, ErrPermissionsScaffold
}

// collectRecord is the uniform per-OS entry point Collect routes through.
func collectRecord(path string, info fs.FileInfo) (*Record, error) {
	return readACL(path, info)
}
