package permissions

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// POSIX ACL xattr binary format (brief §9). This decoder is PURE: it takes the
// raw bytes of a `system.posix_acl_access` / `system.posix_acl_default` xattr
// value and returns a typed structure. It does NOT read the xattr itself — the
// Lgetxattr syscall is the inert OS boundary (permissions_posix.go).
//
// Layout (Linux kernel, <linux/posix_acl_xattr.h>):
//
//	struct posix_acl_xattr_header { __le32 a_version; }              // 4 bytes
//	struct posix_acl_xattr_entry  { __le16 e_tag; __le16 e_perm; __le32 e_id; } // 8 bytes, repeated
//
// a_version MUST be POSIX_ACL_XATTR_VERSION (0x0002). e_id is a uid/gid only for
// ACL_USER/ACL_GROUP entries; otherwise it is ACL_UNDEFINED_ID (0xFFFFFFFF).
const (
	posixACLXattrVersion = 0x0002
	posixACLUndefinedID  = 0xFFFFFFFF

	posixACLHeaderLen = 4
	posixACLEntryLen  = 8
)

// POSIX ACL entry tag types (<linux/posix_acl.h>).
const (
	ACLUserObj  uint16 = 0x01
	ACLUser     uint16 = 0x02
	ACLGroupObj uint16 = 0x04
	ACLGroup    uint16 = 0x08
	ACLMask     uint16 = 0x10
	ACLOther    uint16 = 0x20
)

// POSIX ACL permission bits (e_perm), same rwx values as mode bits.
const (
	aclPermRead    uint16 = 0x4
	aclPermWrite   uint16 = 0x2
	aclPermExecute uint16 = 0x1
)

// Decode errors. A malformed xattr is a per-file, fail-soft condition upstream —
// never a walk-fatal one — but the decoder reports the specific fault so the
// (future) caller can record a precise diagnostic rather than a silent empty ACL.
var (
	// ErrACLBadVersion is returned when the 4-byte header is not version 0x0002.
	ErrACLBadVersion = errors.New("posix acl: unsupported xattr version")
	// ErrACLTruncated is returned when the buffer is too short for the header or
	// its entry payload is not a whole number of 8-byte entries.
	ErrACLTruncated = errors.New("posix acl: truncated xattr value")
)

// PosixACLEntry is one decoded ACL entry, verbatim from the xattr.
type PosixACLEntry struct {
	Tag  uint16 `json:"tag"`
	Perm uint16 `json:"perm"`
	ID   uint32 `json:"id"` // uid/gid for ACLUser/ACLGroup; posixACLUndefinedID otherwise
}

// HasDefinedID reports whether this entry carries a real uid/gid (ACLUser or
// ACLGroup with a non-undefined id).
func (e PosixACLEntry) HasDefinedID() bool {
	return e.ID != posixACLUndefinedID && (e.Tag == ACLUser || e.Tag == ACLGroup)
}

// PosixACL is a decoded POSIX.1e ACL xattr value.
type PosixACL struct {
	Version uint32          `json:"version"`
	Entries []PosixACLEntry `json:"entries"`
}

// DecodePosixACL parses a raw system.posix_acl_access / _default xattr value.
// An empty ACL (header only, no entries) is valid and returns a zero-entry ACL,
// NOT an error. The identical format is used for both the access and default
// ACLs; the caller supplies the scope (§9: the default ACL — who children
// INHERIT — must be captured as a SEPARATE list from the access ACL).
func DecodePosixACL(b []byte) (*PosixACL, error) {
	if len(b) < posixACLHeaderLen {
		return nil, fmt.Errorf("%w: %d bytes, need >= %d for header", ErrACLTruncated, len(b), posixACLHeaderLen)
	}
	version := binary.LittleEndian.Uint32(b[:posixACLHeaderLen])
	if version != posixACLXattrVersion {
		return nil, fmt.Errorf("%w: got 0x%04x, want 0x%04x", ErrACLBadVersion, version, posixACLXattrVersion)
	}
	body := b[posixACLHeaderLen:]
	if len(body)%posixACLEntryLen != 0 {
		return nil, fmt.Errorf("%w: %d entry bytes not a multiple of %d", ErrACLTruncated, len(body), posixACLEntryLen)
	}
	n := len(body) / posixACLEntryLen
	acl := &PosixACL{Version: version, Entries: make([]PosixACLEntry, 0, n)}
	for i := 0; i < n; i++ {
		off := i * posixACLEntryLen
		acl.Entries = append(acl.Entries, PosixACLEntry{
			Tag:  binary.LittleEndian.Uint16(body[off : off+2]),
			Perm: binary.LittleEndian.Uint16(body[off+2 : off+4]),
			ID:   binary.LittleEndian.Uint32(body[off+4 : off+8]),
		})
	}
	return acl, nil
}

// aclTagKind maps an ACL tag to the normalized principal Kind.
func aclTagKind(tag uint16) string {
	switch tag {
	case ACLUserObj, ACLUser:
		return KindUser
	case ACLGroupObj, ACLGroup:
		return KindGroup
	case ACLMask, ACLOther:
		return KindWellKnown
	default:
		return KindUnmapped
	}
}

// ToACEs maps a decoded POSIX ACL into normalized ACEs (all TypeAllow — POSIX.1e
// is additive-only, §1.1). scope is ScopeThis for an access ACL or ScopeDirDefault
// for a default ACL. RawMask preserves the tag+octal-perm verbatim. Principal.ID
// is the numeric qualifier for ACLUser/ACLGroup, else a synthetic tag token
// (e.g. "user_obj") since those entries have no qualifier. This is pure — name
// resolution and well-known classification are layered on by the (future)
// caller, not baked in here.
func (a *PosixACL) ToACEs(scope string) []ACE {
	out := make([]ACE, 0, len(a.Entries))
	for i, e := range a.Entries {
		p := Principal{Kind: aclTagKind(e.Tag)}
		if e.HasDefinedID() {
			p.ID = fmt.Sprintf("%d", e.ID)
		} else {
			p.ID = aclTagToken(e.Tag)
		}
		out = append(out, ACE{
			Principal:  p,
			Type:       TypeAllow,
			Verbs:      PosixRWXToVerbs(e.Perm&0x7, scope == ScopeDirDefault),
			RawMask:    fmt.Sprintf("tag=0x%02x,perm=0%o", e.Tag, e.Perm&0x7),
			Scope:      scope,
			Source:     SourceLocal,
			OrderIndex: i,
		})
	}
	return out
}

// aclTagToken renders a stable token for a tag that has no qualifying id.
func aclTagToken(tag uint16) string {
	switch tag {
	case ACLUserObj:
		return "user_obj"
	case ACLGroupObj:
		return "group_obj"
	case ACLMask:
		return "mask"
	case ACLOther:
		return "other"
	default:
		return fmt.Sprintf("tag_0x%02x", tag)
	}
}
