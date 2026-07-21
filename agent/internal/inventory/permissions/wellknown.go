package permissions

import "strings"

// Well-known principal tags. These are what the central "exclude base/system
// permissions" filter keys on (brief §4). Classification is a STATIC table match
// (no directory lookup) so it still works when name resolution fails (§4).
const (
	WKEveryone           = "Everyone"
	WKCreatorOwner       = "CREATOR OWNER"
	WKCreatorGroup       = "CREATOR GROUP"
	WKSystem             = "SYSTEM"
	WKLocalService       = "LOCAL SERVICE"
	WKNetworkService     = "NETWORK SERVICE"
	WKAdministrators     = "Administrators"
	WKUsers              = "Users"
	WKGuests             = "Guests"
	WKAuthenticatedUsers = "Authenticated Users"

	WKRoot   = "root"
	WKWheel  = "wheel"
	WKDaemon = "daemon"
)

// wellKnownSIDs maps a fixed, OS-version-independent well-known SID string to its
// stable tag (brief §1.2, Microsoft well-known-SID reference). Exact-match only;
// domain-relative RIDs (e.g. Domain Admins S-1-5-21-<domain>-512) are host/domain
// scoped and deliberately NOT in this exact table.
var wellKnownSIDs = map[string]string{
	"S-1-1-0":      WKEveryone,
	"S-1-3-0":      WKCreatorOwner,
	"S-1-3-1":      WKCreatorGroup,
	"S-1-5-18":     WKSystem,
	"S-1-5-19":     WKLocalService,
	"S-1-5-20":     WKNetworkService,
	"S-1-5-11":     WKAuthenticatedUsers,
	"S-1-5-32-544": WKAdministrators,
	"S-1-5-32-545": WKUsers,
	"S-1-5-32-546": WKGuests,
}

// ClassifySID returns the well-known tag for an exact SID string, or "" if the
// SID is not a recognized well-known principal (i.e. a real user/group, whose
// host-vs-domain scope is a separate §2.4 canonicalization concern).
func ClassifySID(sid string) string {
	return wellKnownSIDs[strings.TrimSpace(sid)]
}

// posixWellKnownUID / posixWellKnownGID recognize the common "system" ids by
// number (brief §4). Note the platform nuance captured deliberately: gid 0 is
// "wheel" on BSD/macOS but the "root" group on Linux — both denote the
// superuser group and both are base/system principals a first-run report
// excludes, so a single tag per id is sufficient for the exclusion use case.
var posixWellKnownUID = map[uint32]string{
	0: WKRoot,
	1: WKDaemon,
}

var posixWellKnownGID = map[uint32]string{
	0: WKWheel,
	1: WKDaemon,
}

// posixWellKnownNames recognizes the common system principals by NAME, for the
// path where a uid/gid resolved to a name but the numeric id is not one of the
// fixed ones above (or resolution came from an NFSv4 name@domain string).
var posixWellKnownNames = map[string]string{
	"root":   WKRoot,
	"wheel":  WKWheel,
	"daemon": WKDaemon,
}

// ClassifyPOSIXID returns the well-known tag for a numeric uid (isGroup=false) or
// gid (isGroup=true), or "" if it is an ordinary account.
func ClassifyPOSIXID(id uint32, isGroup bool) string {
	if isGroup {
		return posixWellKnownGID[id]
	}
	return posixWellKnownUID[id]
}

// ClassifyPOSIXName returns the well-known tag for a resolved principal name
// (case-insensitive), or "" if unrecognized. It normalizes both identity
// spellings: a Windows `DOMAIN\user` (take the part after the last backslash)
// and an NFSv4 `user@domain` (take the part before the first at-sign).
func ClassifyPOSIXName(name string) string {
	n := strings.ToLower(strings.TrimSpace(name))
	if i := strings.LastIndex(n, `\`); i >= 0 {
		n = n[i+1:]
	}
	if i := strings.IndexByte(n, '@'); i >= 0 {
		n = n[:i]
	}
	return posixWellKnownNames[n]
}
