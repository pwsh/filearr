package pathspec

import (
	"fmt"
	"path/filepath"
)

// Preset ids — the W6-R1 named folder selections (docs/research/
// agent-inventory-presets.md §5). These are the SAME strings central validates in
// filearr.agent_config.SCAN_PRESET_NAMES.
const (
	PresetUserDocuments    = "user-documents"
	PresetUserMedia        = "user-media"
	PresetUserProfilesFull = "user-profiles-full"
	PresetDownloads        = "downloads"
	PresetServerData       = "server-data"
	PresetCustom           = "custom"
)

// KnownPresets is the resolvable preset set (custom resolves to nothing — its
// roots come entirely from the admin-supplied paths).
var KnownPresets = map[string]bool{
	PresetUserDocuments:    true,
	PresetUserMedia:        true,
	PresetUserProfilesFull: true,
	PresetDownloads:        true,
	PresetServerData:       true,
	PresetCustom:           true,
}

// Host abstracts the per-OS facts preset resolution needs. Splitting them behind
// an interface keeps ResolvePreset PURE and table-testable (a fake Host pretends
// to be any GOOS with mocked known folders / profiles / user-dirs), while
// OSHost() wires the real syscalls. Resolution dispatches on Host.GOOS(), NOT
// runtime.GOOS, so a Windows dev host can exercise the Linux/macOS branches.
type Host interface {
	// GOOS reports the target OS ("windows" | "linux" | "darwin" | ...).
	GOOS() string
	// Home is the current user's home directory.
	Home() (string, error)
	// KnownFolder resolves a Windows known-folder id name ("Documents",
	// "Pictures", "Music", "Videos", "Desktop", "Downloads") to its CURRENT-user
	// path via SHGetKnownFolderPath — KFM-correct (OneDrive Known Folder Move
	// redirects are followed). (path, true) on success; ("", false) on any other
	// OS or an unmapped/failed id.
	KnownFolder(name string) (string, bool)
	// Profiles enumerates the per-user profile roots on this host (C:\Users\*
	// minus reserved names, /home/*, /Users/* minus Shared/Guest). Best-effort:
	// an unreadable users directory yields nil.
	Profiles() []string
	// UserDirs parses a profile home's XDG user-dirs.dirs into var→abs-path. An
	// absent file (minimal/server/WM installs — W6-R1 §2.1) yields an empty map,
	// and the resolver falls back to $HOME.
	UserDirs(home string) map[string]string
}

// ResolvePreset resolves a named preset to its concrete path specs on host h
// (the specs are then fed to Expander, which globs/dedups them). An unknown
// preset is an error; `custom` resolves to no specs (roots come from the
// admin-supplied paths). The per-OS resolution rules are W6-R1 §1–§3.
func ResolvePreset(h Host, preset string) ([]string, error) {
	if !KnownPresets[preset] {
		return nil, fmt.Errorf("unknown preset %q", preset)
	}
	if preset == PresetCustom {
		return nil, nil
	}
	switch h.GOOS() {
	case "windows":
		return resolveWindows(h, preset), nil
	case "linux":
		return resolveLinux(h, preset), nil
	case "darwin":
		return resolveDarwin(h, preset), nil
	default:
		// An unsupported OS resolves to nothing rather than erroring — the command
		// still runs against any explicit paths.
		return nil, nil
	}
}

// resolveWindows resolves a preset to Windows roots (W6-R1 §1). User-data presets
// resolve the CURRENT user's KFM-correct known folder AND each enumerated
// profile's literal `\Folder` join (Expander dedups the overlap); the profile
// join is the pragmatic multi-user path — resolving OTHER users' known folders
// would require loading their registry hives (documented limitation).
func resolveWindows(h Host, preset string) []string {
	var specs []string
	addKF := func(name, leaf string) {
		if p, ok := h.KnownFolder(name); ok {
			specs = append(specs, p)
		}
		for _, prof := range h.Profiles() {
			specs = append(specs, filepath.Join(prof, leaf))
		}
	}
	switch preset {
	case PresetUserDocuments:
		addKF("Documents", "Documents")
	case PresetUserMedia:
		addKF("Pictures", "Pictures")
		addKF("Videos", "Videos")
		addKF("Music", "Music")
	case PresetDownloads:
		addKF("Downloads", "Downloads")
	case PresetUserProfilesFull:
		specs = append(specs, h.Profiles()...)
	case PresetServerData:
		// No FHS equivalent on Windows — intentionally empty (W6-R1 §5).
	}
	return specs
}

// resolveLinux resolves a preset to Linux roots (W6-R1 §2). Per-user folders come
// from each profile's user-dirs.dirs (locale-translated names honored), falling
// back to $HOME itself when the file is absent — never a guessed English name.
func resolveLinux(h Host, preset string) []string {
	var specs []string
	perUser := func(xdgVars []string, fallbackLeaves []string) {
		for _, home := range h.Profiles() {
			dirs := h.UserDirs(home)
			matched := false
			for _, v := range xdgVars {
				if p, ok := dirs[v]; ok && p != "" {
					specs = append(specs, p)
					matched = true
				}
			}
			if !matched {
				if len(fallbackLeaves) == 0 {
					specs = append(specs, home)
				}
				for _, leaf := range fallbackLeaves {
					specs = append(specs, filepath.Join(home, leaf))
				}
			}
		}
	}
	switch preset {
	case PresetUserDocuments:
		// No leaf fallback — fall back to $HOME itself (W6-R1 §2.1).
		perUser([]string{"XDG_DOCUMENTS_DIR"}, nil)
	case PresetUserMedia:
		perUser([]string{"XDG_PICTURES_DIR"}, []string{"Pictures"})
		perUser([]string{"XDG_VIDEOS_DIR"}, []string{"Videos"})
		perUser([]string{"XDG_MUSIC_DIR"}, []string{"Music"})
	case PresetDownloads:
		perUser([]string{"XDG_DOWNLOAD_DIR"}, []string{"Downloads"})
	case PresetUserProfilesFull:
		specs = append(specs, h.Profiles()...)
	case PresetServerData:
		specs = append(specs, "/srv", "/var/www")
	}
	return specs
}

// resolveDarwin resolves a preset to macOS roots (W6-R1 §3). Folder names are
// fixed, non-localized POSIX components (unlike Linux XDG). TCC/Full-Disk-Access
// gating is a walk-time health signal, not a resolution concern.
func resolveDarwin(h Host, preset string) []string {
	var specs []string
	perUser := func(leaves ...string) {
		for _, home := range h.Profiles() {
			for _, leaf := range leaves {
				specs = append(specs, filepath.Join(home, leaf))
			}
		}
	}
	switch preset {
	case PresetUserDocuments:
		perUser("Documents")
	case PresetUserMedia:
		perUser("Pictures", "Movies", "Music")
	case PresetDownloads:
		perUser("Downloads")
	case PresetUserProfilesFull:
		specs = append(specs, h.Profiles()...)
	case PresetServerData:
		// Omitted from the minimum set on macOS (W6-R1 §5).
	}
	return specs
}
