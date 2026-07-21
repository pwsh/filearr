package permissions

import (
	"encoding/binary"
	"errors"
	"testing"
)

// buildACLBytes hand-constructs a POSIX ACL xattr value: a 4-byte LE version
// header followed by 8-byte {tag u16, perm u16, id u32} entries.
func buildACLBytes(version uint32, entries []PosixACLEntry) []byte {
	b := make([]byte, posixACLHeaderLen+len(entries)*posixACLEntryLen)
	binary.LittleEndian.PutUint32(b[:posixACLHeaderLen], version)
	for i, e := range entries {
		off := posixACLHeaderLen + i*posixACLEntryLen
		binary.LittleEndian.PutUint16(b[off:off+2], e.Tag)
		binary.LittleEndian.PutUint16(b[off+2:off+4], e.Perm)
		binary.LittleEndian.PutUint32(b[off+4:off+8], e.ID)
	}
	return b
}

func TestDecodePosixACLValid(t *testing.T) {
	// A typical minimal ACL: user_obj rwx, named user 1001 r-x, group_obj r--,
	// mask r-x, other ---.
	entries := []PosixACLEntry{
		{Tag: ACLUserObj, Perm: 0o7, ID: posixACLUndefinedID},
		{Tag: ACLUser, Perm: 0o5, ID: 1001},
		{Tag: ACLGroupObj, Perm: 0o4, ID: posixACLUndefinedID},
		{Tag: ACLMask, Perm: 0o5, ID: posixACLUndefinedID},
		{Tag: ACLOther, Perm: 0o0, ID: posixACLUndefinedID},
	}
	acl, err := DecodePosixACL(buildACLBytes(posixACLXattrVersion, entries))
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if acl.Version != posixACLXattrVersion {
		t.Fatalf("version: 0x%x", acl.Version)
	}
	if len(acl.Entries) != 5 {
		t.Fatalf("entries: %d", len(acl.Entries))
	}
	named := acl.Entries[1]
	if !named.HasDefinedID() || named.ID != 1001 {
		t.Fatalf("named user entry: %+v", named)
	}
	if acl.Entries[0].HasDefinedID() {
		t.Fatalf("user_obj should have no defined id: %+v", acl.Entries[0])
	}
}

func TestDecodePosixACLEmpty(t *testing.T) {
	// Header only, no entries — valid, zero-entry ACL, NOT an error.
	acl, err := DecodePosixACL(buildACLBytes(posixACLXattrVersion, nil))
	if err != nil {
		t.Fatalf("empty acl should decode: %v", err)
	}
	if len(acl.Entries) != 0 {
		t.Fatalf("expected 0 entries, got %d", len(acl.Entries))
	}
}

func TestDecodePosixACLBadVersion(t *testing.T) {
	b := buildACLBytes(0x0001, []PosixACLEntry{{Tag: ACLUserObj, Perm: 0o7, ID: posixACLUndefinedID}})
	_, err := DecodePosixACL(b)
	if !errors.Is(err, ErrACLBadVersion) {
		t.Fatalf("expected ErrACLBadVersion, got %v", err)
	}
}

func TestDecodePosixACLTruncated(t *testing.T) {
	// Too short for the header.
	if _, err := DecodePosixACL([]byte{0x02, 0x00}); !errors.Is(err, ErrACLTruncated) {
		t.Fatalf("short header: expected ErrACLTruncated, got %v", err)
	}
	// Header ok but a partial (5-byte) entry body.
	b := append(buildACLBytes(posixACLXattrVersion, nil), 0x01, 0x00, 0x07, 0x00, 0xff)
	if _, err := DecodePosixACL(b); !errors.Is(err, ErrACLTruncated) {
		t.Fatalf("partial entry: expected ErrACLTruncated, got %v", err)
	}
}

func TestPosixACLToACEsAccessAndDefault(t *testing.T) {
	entries := []PosixACLEntry{
		{Tag: ACLUserObj, Perm: 0o7, ID: posixACLUndefinedID},
		{Tag: ACLUser, Perm: 0o5, ID: 1001},
		{Tag: ACLOther, Perm: 0o0, ID: posixACLUndefinedID},
	}
	acl, err := DecodePosixACL(buildACLBytes(posixACLXattrVersion, entries))
	if err != nil {
		t.Fatal(err)
	}

	access := acl.ToACEs(ScopeThis)
	if len(access) != 3 {
		t.Fatalf("access aces: %d", len(access))
	}
	// All POSIX.1e entries are additive-only → always allow.
	for i, a := range access {
		if a.Type != TypeAllow {
			t.Fatalf("ace %d not allow: %+v", i, a)
		}
		if a.OrderIndex != i {
			t.Fatalf("ace %d order_index %d", i, a.OrderIndex)
		}
		if a.Scope != ScopeThis || a.Source != SourceLocal {
			t.Fatalf("ace %d scope/source: %+v", i, a)
		}
		if a.RawMask == "" {
			t.Fatalf("ace %d missing raw_mask", i)
		}
	}
	// user_obj has no qualifier → synthetic token; named user → numeric id.
	if access[0].Principal.ID != "user_obj" {
		t.Fatalf("user_obj token: %q", access[0].Principal.ID)
	}
	if access[1].Principal.ID != "1001" || access[1].Principal.Kind != KindUser {
		t.Fatalf("named user: %+v", access[1].Principal)
	}

	// The default ACL is a SEPARATE list, scoped as who children inherit (§9).
	def := acl.ToACEs(ScopeDirDefault)
	if def[0].Scope != ScopeDirDefault {
		t.Fatalf("default scope: %q", def[0].Scope)
	}
}
