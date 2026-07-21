package permissions

import (
	"strings"
)

// MountFidelity classifies what an agent actually SEES when it reads permissions
// through a given mount (brief §2/§6). This is the raw per-mount signal; a
// collector maps it onto the coarser Record.Fidelity (Fidelity* constants) it
// stamps on each emitted entry. Reporting synthesized mode bits as if they were
// a real ACL is the single most misleading failure this feature can produce
// (§2.1), so this classification must ship in v1, not be deferred.
type MountFidelity string

const (
	// MountNative: a real ACL is visible — a local filesystem, or a cifs mount
	// WITH cifsacl (SID-mapped real NTFS ACL).
	MountNative MountFidelity = "native"
	// MountSynthesizedCIFS: a cifs mount WITHOUT cifsacl — every file reports the
	// same fabricated uid/gid/mode from the static mount options, NOT the server
	// ACL. Do not trust as an ACL (§2.1/§2.3).
	MountSynthesizedCIFS MountFidelity = "synthesized_cifs"
	// MountSynthesizedSamba: a client-side FUSE/smbfs projection of a Samba share
	// (heuristic — see DetectMountFidelity). Server-side vfs_acl_xattr synthesis
	// is NOT client-detectable and is a documented gap (§2.1/§6), not this value.
	MountSynthesizedSamba MountFidelity = "synthesized_samba"
	// MountNFS4: an nfs4 client mount — real NFSv4 ACLs, name@domain principals.
	MountNFS4 MountFidelity = "nfs4"
	// MountUnknown: no matching mount, or an fstype this heuristic does not
	// classify (incl. plain NFSv3 — mode bits only, no ACL). Reported honestly
	// rather than guessed.
	MountUnknown MountFidelity = "unknown"
)

// mountEntry is one parsed /proc/mounts (or /proc/self/mountinfo-lite) row.
type mountEntry struct {
	mountPoint string
	fsType     string
	options    string // raw comma-joined options string
}

// localFSTypes are common on-disk filesystems that carry real local ACLs.
var localFSTypes = map[string]bool{
	"ext2": true, "ext3": true, "ext4": true, "xfs": true, "btrfs": true,
	"zfs": true, "reiserfs": true, "jfs": true, "f2fs": true, "overlay": true,
	"tmpfs": true,
}

// DetectMountFidelity parses /proc/mounts-style content and classifies the mount
// that governs path (longest matching mount point wins). PURE: the caller reads
// /proc/mounts (the thin inert boundary); the parse + classify is real. On no
// match it returns MountUnknown (honest), never a fabricated "native".
func DetectMountFidelity(procMounts, path string) MountFidelity {
	me, ok := longestMatch(parseMounts(procMounts), path)
	if !ok {
		return MountUnknown
	}
	return classifyMount(me)
}

// classifyMount applies the §2.3 fidelity table to a single parsed mount.
func classifyMount(me mountEntry) MountFidelity {
	fs := strings.ToLower(me.fsType)
	opts := optionSet(me.options)
	switch {
	case fs == "cifs" || fs == "smb3" || fs == "smb":
		if opts["cifsacl"] {
			return MountNative // real NTFS ACL, SID-mapped via cifs.idmap/winbind/SSSD
		}
		return MountSynthesizedCIFS // fabricated mode from mount options — do not trust
	case fs == "nfs4" || (strings.HasPrefix(fs, "nfs") && nfsIsV4(opts)):
		return MountNFS4
	case fs == "smbfs" || strings.HasPrefix(fs, "fuse.smb"):
		// Heuristic: a userspace SMB projection of a (typically Samba) share.
		return MountSynthesizedSamba
	case localFSTypes[fs]:
		return MountNative
	default:
		return MountUnknown
	}
}

// nfsIsV4 reports whether a generic "nfs" mount is actually NFSv4 (vers=4[.x]).
func nfsIsV4(opts map[string]bool) bool {
	for k := range opts {
		if strings.HasPrefix(k, "vers=4") || strings.HasPrefix(k, "nfsvers=4") {
			return true
		}
	}
	return false
}

// parseMounts splits /proc/mounts content into entries. The format is
// space-separated: `spec mountpoint fstype options dump pass`. Octal \NNN escapes
// (e.g. \040 for space) in the mount point are decoded. Malformed lines are
// skipped rather than failing the parse.
func parseMounts(content string) []mountEntry {
	var out []mountEntry
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		f := strings.Fields(line)
		if len(f) < 4 {
			continue
		}
		out = append(out, mountEntry{
			mountPoint: unescapeMount(f[1]),
			fsType:     f[2],
			options:    f[3],
		})
	}
	return out
}

// longestMatch returns the mount entry whose mount point is the longest path
// prefix of path (with a component boundary), and whether one was found.
func longestMatch(entries []mountEntry, path string) (mountEntry, bool) {
	best := mountEntry{}
	found := false
	for _, e := range entries {
		if pathUnder(path, e.mountPoint) && len(e.mountPoint) >= len(best.mountPoint) {
			best = e
			found = true
		}
	}
	return best, found
}

// pathUnder reports whether path is at or below mount (component-boundary aware,
// so /data does not match /database). Comparison is forward-slash based, matching
// /proc/mounts (Linux-only source).
func pathUnder(path, mount string) bool {
	if mount == "" {
		return false
	}
	if mount == "/" {
		return strings.HasPrefix(path, "/")
	}
	if path == mount {
		return true
	}
	return strings.HasPrefix(path, mount+"/")
}

// optionSet splits a comma-joined mount option string into a set.
func optionSet(options string) map[string]bool {
	set := map[string]bool{}
	for _, o := range strings.Split(options, ",") {
		if o = strings.TrimSpace(o); o != "" {
			set[o] = true
		}
	}
	return set
}

// unescapeMount decodes the octal \NNN escapes /proc/mounts uses for spaces,
// tabs, newlines and backslashes in a mount point.
func unescapeMount(s string) string {
	if !strings.ContainsRune(s, '\\') {
		return s
	}
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] == '\\' && i+4 <= len(s) {
			// \NNN octal (three digits) — the /proc/mounts convention.
			if o, ok := parseOctal3(s[i+1 : i+4]); ok {
				b.WriteByte(o)
				i += 3
				continue
			}
		}
		b.WriteByte(s[i])
	}
	return b.String()
}

// parseOctal3 parses exactly three octal digits into a byte.
func parseOctal3(s string) (byte, bool) {
	if len(s) != 3 {
		return 0, false
	}
	var v int
	for i := 0; i < 3; i++ {
		c := s[i]
		if c < '0' || c > '7' {
			return 0, false
		}
		v = v*8 + int(c-'0')
	}
	return byte(v), true
}
