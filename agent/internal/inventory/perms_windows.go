//go:build windows

package inventory

import (
	"context"
	"io/fs"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

// permsCollector (Windows) summarizes the file's DACL: the ACE count and, per
// trustee, a COMPACT rights string (a short mnemonic set like "R,W,X,D") — NOT a
// full SDDL dump (W6-R1 §6 / the brief's constraint). A nil DACL is the real and
// meaningful "fully permissive" state, reported distinctly from an empty DACL
// ("deny all"). This is a separate security query per file (opt-in cost).
type permsCollector struct{}

func (permsCollector) Name() string { return "perms" }

func (permsCollector) Collect(_ context.Context, path string, _ fs.FileInfo) (map[string]any, error) {
	sd, err := windows.GetNamedSecurityInfo(
		path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION,
	)
	if err != nil {
		return nil, err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return nil, err
	}
	if dacl == nil {
		// A NULL DACL grants everyone full access — a distinct, meaningful state.
		return map[string]any{"dacl": "null (fully permissive)"}, nil
	}
	m := map[string]any{"ace_count": int(dacl.AceCount)}
	var aces []map[string]any
	for i := uint16(0); i < dacl.AceCount; i++ {
		var ace *windows.ACCESS_ALLOWED_ACE
		if err := windows.GetAce(dacl, uint32(i), &ace); err != nil {
			continue
		}
		// The SID starts at &ace.SidStart (the ACE is variable-length; the SID is
		// inlined at that offset).
		sid := (*windows.SID)(unsafe.Pointer(&ace.SidStart))
		entry := map[string]any{
			"type":   aceTypeName(ace.Header.AceType),
			"rights": compactRights(uint32(ace.Mask)),
		}
		if sid.IsValid() {
			entry["sid"] = sid.String()
			if account, domain, _, aerr := sid.LookupAccount(""); aerr == nil {
				if domain != "" {
					entry["trustee"] = domain + "\\" + account
				} else {
					entry["trustee"] = account
				}
			}
		}
		aces = append(aces, entry)
	}
	if len(aces) > 0 {
		m["aces"] = aces
	}
	return m, nil
}

func aceTypeName(t uint8) string {
	switch t {
	case windows.ACCESS_ALLOWED_ACE_TYPE:
		return "allow"
	case windows.ACCESS_DENIED_ACE_TYPE:
		return "deny"
	default:
		return "other"
	}
}

// compactRights renders an access mask as a short mnemonic set, coarsely
// bucketed to the generic/standard rights an admin scans for at a glance.
func compactRights(mask uint32) string {
	var parts []string
	add := func(bit uint32, name string) {
		if mask&bit == bit {
			parts = append(parts, name)
		}
	}
	add(windows.GENERIC_ALL, "GA")
	add(windows.GENERIC_READ, "GR")
	add(windows.GENERIC_WRITE, "GW")
	add(windows.GENERIC_EXECUTE, "GX")
	add(windows.FILE_GENERIC_READ, "R")
	add(windows.FILE_GENERIC_WRITE, "W")
	add(windows.FILE_GENERIC_EXECUTE, "X")
	add(windows.DELETE, "D")
	add(windows.WRITE_DAC, "WDAC")
	add(windows.WRITE_OWNER, "WO")
	if len(parts) == 0 {
		return "0x" + strings.TrimLeft(hex32(mask), "0")
	}
	return strings.Join(parts, ",")
}

func hex32(v uint32) string {
	const digits = "0123456789abcdef"
	var b [8]byte
	for i := 7; i >= 0; i-- {
		b[i] = digits[v&0xf]
		v >>= 4
	}
	return string(b[:])
}
