package shares

import (
	"reflect"
	"testing"
)

// These tests exercise the PURE parsers from canned OS output fixtures — they do
// NOT depend on the host's real shares (R1), so they run identically on every CI
// platform. Live enumeration is only exercised behind the FILEARR_TEST_LIVE_SHARES
// env flag (see TestLiveEnumerateOptIn).

func TestParseSmbShareCSV(t *testing.T) {
	out := `"Name","Path"
"media","D:\media"
"Public Files","C:\Users\Public\Shared Files"
"C$","C:\"
"ADMIN$","C:\Windows"
"IPC$",""
`
	got := parseSmbShareCSV(out)
	want := []export{
		{name: "media", path: `D:\media`, kind: "smb"},
		{name: "Public Files", path: `C:\Users\Public\Shared Files`, kind: "smb"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("parseSmbShareCSV = %+v, want %+v", got, want)
	}
}

func TestParseNetShare(t *testing.T) {
	out := "Share name   Resource                        Remark\r\n" +
		"-------------------------------------------------------------\r\n" +
		"C$           C:\\                             Default share\r\n" +
		"media        D:\\media\r\n" +
		"IPC$                                         Remote IPC\r\n" +
		"ADMIN$       C:\\Windows                      Remote Admin\r\n" +
		"The command completed successfully.\r\n"
	got := parseNetShare(out)
	want := []export{{name: "media", path: `D:\media`, kind: "smb"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("parseNetShare = %+v, want %+v", got, want)
	}
}

func TestParseSmbConf(t *testing.T) {
	content := `# main config
[global]
   workgroup = WORKGROUP
   security = user

[media]
   path = /srv/media
   read only = yes

[backup]
   directory = /srv/backup

[printers]
   path = /var/spool/samba
   printable = yes

[scratch]
   comment = no path here
`
	got := parseSmbConf(content)
	want := []export{
		{name: "media", path: "/srv/media", kind: "smb"},
		{name: "backup", path: "/srv/backup", kind: "smb"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("parseSmbConf = %+v, want %+v", got, want)
	}
}

func TestParseExports(t *testing.T) {
	content := `# /etc/exports
/srv/nfs/media  192.168.1.0/24(rw,sync,no_subtree_check)
/srv/nfs/backup *(ro)
"/srv/with space/data" host1(rw)
relative/not/absolute host(rw)
`
	got := parseExports(content)
	want := []export{
		{path: "/srv/nfs/media", kind: "nfs"},
		{path: "/srv/nfs/backup", kind: "nfs"},
		{path: "/srv/with space/data", kind: "nfs"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("parseExports = %+v, want %+v", got, want)
	}
}

func TestParseSharingL(t *testing.T) {
	out := `List of Share Points

name:		Media
path:		/Volumes/Data/Media
afp:		disabled
smb:		enabled

name:		Public Share
path:		/Users/eric/Public
smb:		enabled
`
	got := parseSharingL(out)
	want := []export{
		{name: "Media", path: "/Volumes/Data/Media", kind: "smb"},
		{name: "Public Share", path: "/Users/eric/Public", kind: "smb"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("parseSharingL = %+v, want %+v", got, want)
	}
}

func TestParseEmptyAndGarbageAreTolerant(t *testing.T) {
	// R1: malformed / empty input never panics and yields no exports.
	for _, in := range []string{"", "   \n\n", "not a valid\nshare listing"} {
		if ex := parseSmbShareCSV(in); len(ex) != 0 {
			t.Errorf("parseSmbShareCSV(%q) = %+v, want empty", in, ex)
		}
		if ex := parseSmbConf(in); len(ex) != 0 {
			t.Errorf("parseSmbConf(%q) = %+v, want empty", in, ex)
		}
		if ex := parseExports(in); len(ex) != 0 {
			t.Errorf("parseExports(%q) = %+v, want empty", in, ex)
		}
	}
}
