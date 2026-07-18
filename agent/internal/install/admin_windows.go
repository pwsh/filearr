//go:build windows

package install

import "golang.org/x/sys/windows"

// IsAdmin reports whether the current process token is a member of the local
// Administrators group (the standard elevation check), the precondition for
// registering a Windows service and writing under %ProgramFiles% / %ProgramData%.
func IsAdmin() bool {
	var adminSID *windows.SID
	// S-1-5-32-544 (BUILTIN\Administrators).
	err := windows.AllocateAndInitializeSid(
		&windows.SECURITY_NT_AUTHORITY,
		2,
		windows.SECURITY_BUILTIN_DOMAIN_RID,
		windows.DOMAIN_ALIAS_RID_ADMINS,
		0, 0, 0, 0, 0, 0,
		&adminSID,
	)
	if err != nil {
		return false
	}
	defer windows.FreeSid(adminSID)

	// A zero token handle means "use the current process token" for IsMember.
	member, err := windows.Token(0).IsMember(adminSID)
	return err == nil && member
}
