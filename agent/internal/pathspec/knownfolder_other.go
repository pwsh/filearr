//go:build !windows

package pathspec

// osKnownFolder is a no-op off Windows: known folders are a Windows-shell concept
// (SHGetKnownFolderPath). Linux/macOS presets resolve via XDG / fixed paths.
func osKnownFolder(name string) (string, bool) { return "", false }
