package permissions

import (
	"reflect"
	"testing"
)

func TestNTFSMaskToVerbs(t *testing.T) {
	// FILE_GENERIC_READ = READ_CONTROL|FILE_READ_DATA|FILE_READ_ATTRIBUTES|FILE_READ_EA|SYNCHRONIZE
	fileGenericRead := ntfsReadControl | ntfsFileReadData | ntfsFileReadAttributes | ntfsFileReadEA | ntfsSynchronize

	tests := []struct {
		name  string
		mask  uint32
		isDir bool
		want  []string
	}{
		{"read_data file", ntfsFileReadData, false, []string{VerbRead}},
		{"read_data dir adds list", ntfsFileReadData, true, []string{VerbRead, VerbList}},
		{"write_data", ntfsFileWriteData, false, []string{VerbWrite}},
		{"delete + change_perms + take_own", ntfsDelete | ntfsWriteDAC | ntfsWriteOwner, false,
			[]string{VerbDelete, VerbChangePerms, VerbTakeOwn}},
		{"synchronize alone drops to nothing", ntfsSynchronize, false, []string{}},
		{"file_generic_read expands", fileGenericRead, false,
			[]string{VerbRead, VerbReadAttr, VerbReadPerms}},
		// Generic-rights mapping (the explicitly-required case).
		{"GENERIC_READ", ntfsGenericRead, false, []string{VerbRead, VerbReadAttr, VerbReadPerms}},
		{"GENERIC_WRITE", ntfsGenericWrite, false,
			[]string{VerbWrite, VerbAppend, VerbWriteAttr, VerbReadPerms}},
		{"GENERIC_EXECUTE", ntfsGenericExec, false, []string{VerbExecute, VerbReadAttr, VerbReadPerms}},
		{"GENERIC_ALL file", ntfsGenericAll, false, []string{
			VerbFull, VerbRead, VerbWrite, VerbExecute, VerbAppend,
			VerbDelete, VerbReadAttr, VerbWriteAttr, VerbReadPerms, VerbChangePerms, VerbTakeOwn}},
		{"GENERIC_ALL dir includes list+delete_child", ntfsGenericAll, true, verbOrder},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := NTFSMaskToVerbs(tt.mask, tt.isDir)
			if !reflect.DeepEqual(got, tt.want) {
				t.Fatalf("NTFSMaskToVerbs(0x%x, dir=%v) = %v, want %v", tt.mask, tt.isDir, got, tt.want)
			}
		})
	}
}

func TestPosixRWXToVerbs(t *testing.T) {
	tests := []struct {
		name  string
		rwx   uint16
		isDir bool
		want  []string
	}{
		{"rwx file", 0o7, false, []string{VerbRead, VerbWrite, VerbExecute}},
		{"rwx dir adds list", 0o7, true, []string{VerbRead, VerbWrite, VerbExecute, VerbList}},
		{"r-- ", 0o4, false, []string{VerbRead}},
		{"-w-", 0o2, false, []string{VerbWrite}},
		{"--x file", 0o1, false, []string{VerbExecute}},
		{"--x dir traversable", 0o1, true, []string{VerbExecute, VerbList}},
		{"no bits", 0o0, false, []string{}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := PosixRWXToVerbs(tt.rwx, tt.isDir)
			if !reflect.DeepEqual(got, tt.want) {
				t.Fatalf("PosixRWXToVerbs(0%o, dir=%v) = %v, want %v", tt.rwx, tt.isDir, got, tt.want)
			}
		})
	}
}

func TestNFSv4MaskToVerbs(t *testing.T) {
	got := NFSv4MaskToVerbs(nfs4ReadData|nfs4WriteData|nfs4WriteACL, false)
	want := []string{VerbRead, VerbWrite, VerbChangePerms}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("NFSv4MaskToVerbs = %v, want %v", got, want)
	}
	// Directory alias of READ_DATA.
	got = NFSv4MaskToVerbs(nfs4ReadData, true)
	want = []string{VerbRead, VerbList}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("NFSv4MaskToVerbs dir = %v, want %v", got, want)
	}
}

func TestOrderVerbsIsStable(t *testing.T) {
	// A set built in arbitrary order must emit in verbOrder.
	set := map[string]bool{VerbTakeOwn: true, VerbRead: true, VerbFull: true, VerbList: true}
	got := orderVerbs(set)
	want := []string{VerbFull, VerbRead, VerbList, VerbTakeOwn}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("orderVerbs = %v, want %v", got, want)
	}
}
