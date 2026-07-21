package permissions

// Normalized verb vocabulary (brief §3.2). RawMask on every ACE is the source of
// truth; these verbs are the queryable, cross-OS projection ("who has read"
// uniformly across DACL / POSIX mode / POSIX ACL / NFSv4).
const (
	VerbFull        = "full"
	VerbRead        = "read"
	VerbWrite       = "write"
	VerbExecute     = "execute"
	VerbAppend      = "append"
	VerbList        = "list"
	VerbDelete      = "delete"
	VerbDeleteChild = "delete_child"
	VerbReadAttr    = "read_attr"
	VerbWriteAttr   = "write_attr"
	VerbReadPerms   = "read_perms"
	VerbChangePerms = "change_perms"
	VerbTakeOwn     = "take_own"
)

// verbOrder is the STABLE emission order for a verb set (deterministic output for
// diffing and tests). Any verb produced by a mapping table must appear here.
var verbOrder = []string{
	VerbFull, VerbRead, VerbWrite, VerbExecute, VerbAppend, VerbList,
	VerbDelete, VerbDeleteChild, VerbReadAttr, VerbWriteAttr,
	VerbReadPerms, VerbChangePerms, VerbTakeOwn,
}

// orderVerbs returns the set as a slice in verbOrder (stable, deduped).
func orderVerbs(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for _, v := range verbOrder {
		if set[v] {
			out = append(out, v)
		}
	}
	return out
}

// --- Windows NTFS access mask (brief §3.2, standard winnt.h values) ---
const (
	ntfsFileReadData        uint32 = 0x0001 // FILE_LIST_DIRECTORY is the directory alias of this bit
	ntfsFileWriteData       uint32 = 0x0002
	ntfsFileAppendData      uint32 = 0x0004
	ntfsFileReadEA          uint32 = 0x0008
	ntfsFileWriteEA         uint32 = 0x0010
	ntfsFileExecute         uint32 = 0x0020
	ntfsFileDeleteChild     uint32 = 0x0040
	ntfsFileReadAttributes  uint32 = 0x0080
	ntfsFileWriteAttributes uint32 = 0x0100

	ntfsDelete       uint32 = 0x00010000
	ntfsReadControl  uint32 = 0x00020000
	ntfsWriteDAC     uint32 = 0x00040000
	ntfsWriteOwner   uint32 = 0x00080000
	ntfsSynchronize  uint32 = 0x00100000 // dropped: not a meaningful access grant for reporting (§3.2)
	ntfsGenericAll   uint32 = 0x10000000
	ntfsGenericExec  uint32 = 0x20000000
	ntfsGenericWrite uint32 = 0x40000000
	ntfsGenericRead  uint32 = 0x80000000
)

// ntfsSpecificBits maps a single specific (non-generic) NTFS mask bit to a verb.
var ntfsSpecificBits = []struct {
	bit  uint32
	verb string
}{
	{ntfsFileReadData, VerbRead}, // list alias handled by NTFSMaskToVerbs when isDir
	{ntfsFileWriteData, VerbWrite},
	{ntfsFileAppendData, VerbAppend},
	{ntfsFileExecute, VerbExecute},
	{ntfsFileDeleteChild, VerbDeleteChild},
	{ntfsDelete, VerbDelete},
	{ntfsReadControl, VerbReadPerms},
	{ntfsWriteDAC, VerbChangePerms},
	{ntfsWriteOwner, VerbTakeOwn},
	{ntfsFileReadAttributes, VerbReadAttr},
	{ntfsFileReadEA, VerbReadAttr},
	{ntfsFileWriteAttributes, VerbWriteAttr},
	{ntfsFileWriteEA, VerbWriteAttr},
}

// NTFSMaskToVerbs maps an NTFS/ReFS access mask to the normalized verb set.
// Generic rights (GENERIC_*) are expanded in the spirit of MapGenericMask — this
// is the "generic_mapping_applied" posture the schema records. GENERIC_ALL yields
// VerbFull (plus the full expanded set for uniform querying). isDir selects the
// directory alias of FILE_READ_DATA (list vs read). RawMask on the ACE preserves
// the verbatim mask regardless.
func NTFSMaskToVerbs(mask uint32, isDir bool) []string {
	set := map[string]bool{}

	if mask&ntfsGenericAll == ntfsGenericAll {
		// Full control: every verb (VerbFull is the canonical marker).
		for _, v := range verbOrder {
			set[v] = true
		}
		if !isDir {
			delete(set, VerbList)
			delete(set, VerbDeleteChild)
		}
		return orderVerbs(set)
	}
	// FILE_GENERIC_READ = READ_CONTROL|FILE_READ_DATA|FILE_READ_ATTRIBUTES|FILE_READ_EA|SYNCHRONIZE
	if mask&ntfsGenericRead == ntfsGenericRead {
		set[VerbRead], set[VerbReadAttr], set[VerbReadPerms] = true, true, true
		if isDir {
			set[VerbList] = true
		}
	}
	// FILE_GENERIC_WRITE = READ_CONTROL|FILE_WRITE_DATA|FILE_APPEND_DATA|FILE_WRITE_ATTRIBUTES|FILE_WRITE_EA|SYNCHRONIZE
	if mask&ntfsGenericWrite == ntfsGenericWrite {
		set[VerbWrite], set[VerbAppend], set[VerbWriteAttr], set[VerbReadPerms] = true, true, true, true
	}
	// FILE_GENERIC_EXECUTE = READ_CONTROL|FILE_EXECUTE|FILE_READ_ATTRIBUTES|SYNCHRONIZE
	if mask&ntfsGenericExec == ntfsGenericExec {
		set[VerbExecute], set[VerbReadAttr], set[VerbReadPerms] = true, true, true
	}

	for _, m := range ntfsSpecificBits {
		if mask&m.bit == m.bit {
			set[m.verb] = true
		}
	}
	if isDir && mask&ntfsFileReadData == ntfsFileReadData {
		set[VerbList] = true // directory alias of FILE_READ_DATA (FILE_LIST_DIRECTORY)
	}
	return orderVerbs(set)
}

// --- POSIX mode bits (brief §3.2) ---
const (
	posixRead    uint16 = 0x4 // r
	posixWrite   uint16 = 0x2 // w
	posixExecute uint16 = 0x1 // x
)

// PosixRWXToVerbs maps a single 3-bit rwx triplet (0..7 — one class's bits) to
// verbs. On a directory, execute additionally implies list/traverse (a
// well-known POSIX quirk, §3.2). No delete/change_perms/take_own verb is
// synthesized from a file's own mode: POSIX delete is governed by the CONTAINING
// directory's write+execute, not the file's mode (§3.2) — inventing one here
// would misrepresent the model.
func PosixRWXToVerbs(rwx uint16, isDir bool) []string {
	set := map[string]bool{}
	if rwx&posixRead == posixRead {
		set[VerbRead] = true
	}
	if rwx&posixWrite == posixWrite {
		set[VerbWrite] = true
	}
	if rwx&posixExecute == posixExecute {
		set[VerbExecute] = true
		if isDir {
			set[VerbList] = true
		}
	}
	return orderVerbs(set)
}

// --- NFSv4 / macOS extended-ACL mask (brief §3.2 — STUB, documented, partial) ---
//
// TODO(W7-T4): flesh out and verify against RFC 8881 §6.2.1.3.2 (NFSv4) and
// macOS <sys/acl.h> before the darwin/nfs4 reads land. The NFSv4 ACE mask bit
// positions closely parallel the Windows FILE_* lineage BY DESIGN, but the
// values below are the NFSv4 canonical bits, NOT the winnt.h ones, and this
// table is intentionally PARTIAL: RawMask preservation is the guarantee, verb
// coverage here is best-effort until T4 verifies every bit.
const (
	nfs4ReadData        uint32 = 0x00000001 // also ListDirectory on a dir
	nfs4WriteData       uint32 = 0x00000002 // also AddFile on a dir
	nfs4AppendData      uint32 = 0x00000004 // also AddSubdirectory on a dir
	nfs4Execute         uint32 = 0x00000020
	nfs4DeleteChild     uint32 = 0x00000040
	nfs4ReadAttributes  uint32 = 0x00000080
	nfs4WriteAttributes uint32 = 0x00000100
	nfs4Delete          uint32 = 0x00010000
	nfs4ReadACL         uint32 = 0x00020000
	nfs4WriteACL        uint32 = 0x00040000
	nfs4WriteOwner      uint32 = 0x00080000
)

var nfs4Bits = []struct {
	bit  uint32
	verb string
}{
	{nfs4ReadData, VerbRead},
	{nfs4WriteData, VerbWrite},
	{nfs4AppendData, VerbAppend},
	{nfs4Execute, VerbExecute},
	{nfs4DeleteChild, VerbDeleteChild},
	{nfs4ReadAttributes, VerbReadAttr},
	{nfs4WriteAttributes, VerbWriteAttr},
	{nfs4Delete, VerbDelete},
	{nfs4ReadACL, VerbReadPerms},
	{nfs4WriteACL, VerbChangePerms},
	{nfs4WriteOwner, VerbTakeOwn},
}

// NFSv4MaskToVerbs maps an NFSv4/macOS-extended ACE mask to normalized verbs.
// STUB (see nfs4Bits TODO): correct for the common bits, intentionally partial.
// isDir selects the list alias of nfs4ReadData.
func NFSv4MaskToVerbs(mask uint32, isDir bool) []string {
	set := map[string]bool{}
	for _, m := range nfs4Bits {
		if mask&m.bit == m.bit {
			set[m.verb] = true
		}
	}
	if isDir && mask&nfs4ReadData == nfs4ReadData {
		set[VerbList] = true
	}
	return orderVerbs(set)
}
