//go:build windows

package pathspec

import "golang.org/x/sys/windows"

// winKnownFolders maps the W6-R1 §1.1 folder names to their stable KNOWNFOLDERID
// GUIDs. Resolving via SHGetKnownFolderPath (not a `%USERPROFILE%\Documents`
// string) is what makes an OneDrive Known-Folder-Move redirect resolve to the
// real synced path instead of a stale empty leftover.
var winKnownFolders = map[string]*windows.KNOWNFOLDERID{
	"Documents": windows.FOLDERID_Documents,
	"Pictures":  windows.FOLDERID_Pictures,
	"Music":     windows.FOLDERID_Music,
	"Videos":    windows.FOLDERID_Videos,
	"Desktop":   windows.FOLDERID_Desktop,
	"Downloads": windows.FOLDERID_Downloads,
}

// osKnownFolder resolves a known-folder name to the CURRENT user's path.
// KF_FLAG_DEFAULT does not create the folder and does not verify existence — it
// returns the configured (possibly redirected) path, which is exactly what an
// inventory root wants.
func osKnownFolder(name string) (string, bool) {
	id, ok := winKnownFolders[name]
	if !ok {
		return "", false
	}
	p, err := windows.KnownFolderPath(id, windows.KF_FLAG_DEFAULT)
	if err != nil || p == "" {
		return "", false
	}
	return p, true
}
