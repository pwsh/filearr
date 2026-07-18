package pathspec

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// osHost is the production Host: real env/home, filepath.Glob profile
// enumeration, and the build-tagged known-folder resolver (osKnownFolder).
type osHost struct{}

// OSHost returns the production Host for the running platform.
func OSHost() Host { return osHost{} }

func (osHost) GOOS() string { return runtime.GOOS }

func (osHost) Home() (string, error) { return os.UserHomeDir() }

func (osHost) KnownFolder(name string) (string, bool) { return osKnownFolder(name) }

func (osHost) UserDirs(home string) map[string]string {
	b, err := os.ReadFile(filepath.Join(home, ".config", "user-dirs.dirs"))
	if err != nil {
		return map[string]string{}
	}
	return parseUserDirs(string(b), home)
}

// Profiles enumerates per-user profile roots per platform. Best-effort: any error
// (unreadable users dir) yields nil, and the caller falls back to whatever
// current-user known folder / explicit paths it has.
//
// This is the FILESYSTEM-glob fallback path W6-R1 flags (§1.3 registry-first on
// Windows, §2.2 passwd-first on Linux): the authoritative registry/passwd
// enumeration is a documented future refinement; the glob (with the reserved-name
// exclusions W6-R1 lists) is adequate for the common single-or-few-account host.
func (osHost) Profiles() []string {
	switch runtime.GOOS {
	case "windows":
		drive := os.Getenv("SystemDrive")
		if drive == "" {
			drive = "C:"
		}
		return globProfiles(filepath.Join(drive+`\`, "Users"), winReserved)
	case "darwin":
		return globProfiles("/Users", darwinReserved)
	case "linux":
		return globProfiles("/home", nil)
	default:
		return nil
	}
}

// winReserved / darwinReserved are the profile-glob exclusion sets (W6-R1 §1.3 /
// §3.1): system/template/shared pseudo-profiles a bare `*` glob cannot distinguish
// from a real user. A name ending in `$` (service-account convention) is also
// dropped on Windows.
var winReserved = map[string]bool{
	"Default": true, "Default User": true, "Public": true, "All Users": true,
	"DefaultAppPool": true, "WDAGUtilityAccount": true,
}

var darwinReserved = map[string]bool{"Shared": true, "Guest": true}

func globProfiles(usersDir string, reserved map[string]bool) []string {
	entries, err := os.ReadDir(usersDir)
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range entries {
		if !e.IsDir() {
			// A directory OR a symlink to one (ReadDir reports the symlink type);
			// only skip plain files.
			if e.Type()&os.ModeSymlink == 0 {
				continue
			}
		}
		name := e.Name()
		if reserved[name] || strings.HasSuffix(name, "$") {
			continue
		}
		out = append(out, filepath.Join(usersDir, name))
	}
	return out
}
