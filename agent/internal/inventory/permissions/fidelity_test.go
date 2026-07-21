package permissions

import "testing"

// A representative /proc/mounts fixture spanning the §2.3 fidelity table.
const procMountsFixture = `sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
/dev/sda1 / ext4 rw,relatime 0 0
/dev/sdb1 /data xfs rw,relatime 0 0
//nas/share /mnt/cifsacl cifs rw,relatime,vers=3.1.1,cifsacl,uid=0 0 0
//nas/plain /mnt/plaincifs cifs rw,relatime,vers=3.0,uid=1000,gid=1000,file_mode=0755 0 0
nas:/export /mnt/nfs4 nfs4 rw,relatime,vers=4.2 0 0
oldnas:/export /mnt/nfs3 nfs rw,relatime,vers=3 0 0
smbnetfs /mnt/samba fuse.smbnetfs rw,nosuid,nodev,relatime 0 0
weird /mnt/weird someunknownfs rw 0 0
`

func TestDetectMountFidelity(t *testing.T) {
	tests := []struct {
		path string
		want MountFidelity
	}{
		{"/data/movies/a.mkv", MountNative},            // local xfs
		{"/home/user/x", MountNative},                  // falls to root ext4
		{"/mnt/cifsacl/dir/f", MountNative},            // cifs WITH cifsacl
		{"/mnt/plaincifs/dir/f", MountSynthesizedCIFS}, // cifs WITHOUT cifsacl
		{"/mnt/nfs4/share/f", MountNFS4},               // nfs4
		{"/mnt/nfs3/share/f", MountUnknown},            // nfsv3: mode-only, no ACL
		{"/mnt/samba/f", MountSynthesizedSamba},        // fuse smb projection
		{"/mnt/weird/f", MountUnknown},                 // unclassified fstype
	}
	for _, tt := range tests {
		if got := DetectMountFidelity(procMountsFixture, tt.path); got != tt.want {
			t.Errorf("DetectMountFidelity(%q) = %q, want %q", tt.path, got, tt.want)
		}
	}
}

func TestDetectMountFidelityLongestPrefix(t *testing.T) {
	// /data/deep is a more specific mount than / — it must win.
	mounts := "/dev/sda1 / ext4 rw 0 0\n//nas/x /data/deep cifs rw,uid=0 0 0\n"
	if got := DetectMountFidelity(mounts, "/data/deep/f"); got != MountSynthesizedCIFS {
		t.Fatalf("longest-prefix: got %q, want synthesized_cifs", got)
	}
	if got := DetectMountFidelity(mounts, "/data/other/f"); got != MountNative {
		t.Fatalf("sibling path should resolve to root ext4: got %q", got)
	}
}

func TestDetectMountFidelityNoMatch(t *testing.T) {
	if got := DetectMountFidelity("", "/anything"); got != MountUnknown {
		t.Fatalf("empty mounts: got %q, want unknown", got)
	}
}

func TestComponentBoundaryNoFalsePrefix(t *testing.T) {
	// /database must NOT match the /data mount.
	mounts := "/dev/sda1 / ext4 rw 0 0\n//nas/x /data cifs rw,uid=0 0 0\n"
	if got := DetectMountFidelity(mounts, "/database/f"); got != MountNative {
		t.Fatalf("/database should match root, not /data: got %q", got)
	}
}

func TestUnescapeMount(t *testing.T) {
	// /proc/mounts encodes a space in a mount point as \040.
	mounts := "//nas/x /mnt/my\\040share cifs rw,uid=0 0 0\n/dev/sda1 / ext4 rw 0 0\n"
	if got := DetectMountFidelity(mounts, "/mnt/my share/f"); got != MountSynthesizedCIFS {
		t.Fatalf("escaped mountpoint: got %q, want synthesized_cifs", got)
	}
}
