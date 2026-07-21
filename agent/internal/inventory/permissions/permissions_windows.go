//go:build windows

package permissions

import "io/fs"

// readSecurityDescriptor is the INERT Windows ACL read seam.
//
// TODO(W7-T3): implement via golang.org/x/sys/windows.GetNamedSecurityInfo(path,
// SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION|GROUP_SECURITY_INFORMATION|
// DACL_SECURITY_INFORMATION [|SACL_SECURITY_INFORMATION when SeSecurityPrivilege
// is held]) — works transparently on local and UNC paths (§1.2). Walk the full
// DACL with windows.GetAce, reading ace.Header.AceFlags for the
// INHERITED_ACE/OBJECT_INHERIT/CONTAINER_INHERIT/INHERIT_ONLY/NO_PROPAGATE bits
// (the existing perms_windows.go reads only AceType, not AceFlags), map ace.Mask
// via NTFSMaskToVerbs, classify the SID via ClassifySID, and populate *Record
// (Posture.DaclPresent/DaclCanonical, Fidelity=FidelityFullNative). An
// ERROR_PRIVILEGE_NOT_HELD on the SACL must surface as a distinct health state,
// never an unhandled error. NO real syscall is issued here yet.
func readSecurityDescriptor(path string) (*Record, error) {
	_ = path
	return nil, ErrPermissionsScaffold
}

// collectRecord is the uniform per-OS entry point Collect routes through.
func collectRecord(path string, _ fs.FileInfo) (*Record, error) {
	return readSecurityDescriptor(path)
}
