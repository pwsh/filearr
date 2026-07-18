//go:build windows

package inventory

import (
	"context"
	"io/fs"

	"golang.org/x/sys/windows"
)

// ownerCollector (Windows) queries the file's owner SID via GetNamedSecurityInfo
// (a separate, explicit security query — NOT part of the walk's stat) and
// best-effort resolves it to an account name. Per W6-R1 §6 the raw SID is always
// reported; the LookupAccountSid name resolution (a possible domain-controller
// round trip) is best-effort and reported by omission on failure.
type ownerCollector struct{}

func (ownerCollector) Name() string { return "owner" }

func (ownerCollector) Collect(_ context.Context, path string, _ fs.FileInfo) (map[string]any, error) {
	sd, err := windows.GetNamedSecurityInfo(
		path, windows.SE_FILE_OBJECT, windows.OWNER_SECURITY_INFORMATION,
	)
	if err != nil {
		return nil, err
	}
	owner, _, err := sd.Owner()
	if err != nil || owner == nil {
		return nil, err
	}
	m := map[string]any{"owner_sid": owner.String()}
	if account, domain, _, aerr := owner.LookupAccount(""); aerr == nil {
		if domain != "" {
			m["owner"] = domain + "\\" + account
		} else {
			m["owner"] = account
		}
	}
	return m, nil
}
