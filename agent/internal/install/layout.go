// Package install resolves the per-OS installation layout and drives the
// idempotent install/uninstall of the agent as a system service. The OS side
// effects (filesystem, privilege check, service registration) are behind small
// injectable interfaces so the layout math and the install/uninstall decision
// logic are unit-testable without touching a real machine.
package install

import "fmt"

// Layout is the resolved set of install paths for one OS.
type Layout struct {
	// InstallDir holds the binary. BinPath is the binary itself.
	InstallDir string
	BinPath    string
	// DataDir is the agent's per-machine state (keys/cert/index/outbox), the
	// data_dir default under a service install.
	DataDir string
	// ConfigDir holds the user-editable sidecar; ConfigPath is that file.
	ConfigDir  string
	ConfigPath string
	// LogDir is the generic logs folder the daemon writes filearr-agent.log into.
	LogDir string
}

// SidecarFileName is the sidecar file placed under ConfigDir (kept in sync with
// sidecar.FileName; duplicated as a const to avoid an import cycle risk and to
// keep this package dependency-light).
const SidecarFileName = "filearr-agent.json"

// ResolveLayout computes the install layout for goos, reading %ProgramFiles% and
// %ProgramData% (Windows) from getenv. Paths use the TARGET OS separator so the
// result is correct when cross-resolving (e.g. a Windows host computing the
// linux layout in a test). getenv may be nil (defaults are then used).
func ResolveLayout(goos string, getenv func(string) string) (Layout, error) {
	if getenv == nil {
		getenv = func(string) string { return "" }
	}
	switch goos {
	case "windows":
		programFiles := getenv("ProgramFiles")
		if programFiles == "" {
			programFiles = `C:\Program Files`
		}
		programData := getenv("ProgramData")
		if programData == "" {
			programData = `C:\ProgramData`
		}
		installDir := programFiles + `\Filearr Agent`
		dataDir := programData + `\Filearr Agent`
		return Layout{
			InstallDir: installDir,
			BinPath:    installDir + `\filearr-agent.exe`,
			DataDir:    dataDir,
			ConfigDir:  dataDir,
			ConfigPath: dataDir + `\` + SidecarFileName,
			LogDir:     dataDir + `\logs`,
		}, nil
	case "darwin":
		const support = "/Library/Application Support/FilearrAgent"
		return Layout{
			InstallDir: "/usr/local/bin",
			BinPath:    "/usr/local/bin/filearr-agent",
			DataDir:    support,
			ConfigDir:  support,
			ConfigPath: support + "/" + SidecarFileName,
			LogDir:     "/Library/Logs/FilearrAgent",
		}, nil
	case "linux":
		return Layout{
			InstallDir: "/usr/local/bin",
			BinPath:    "/usr/local/bin/filearr-agent",
			DataDir:    "/var/lib/filearr-agent",
			ConfigDir:  "/etc/filearr-agent",
			ConfigPath: "/etc/filearr-agent/" + SidecarFileName,
			LogDir:     "/var/log/filearr-agent",
		}, nil
	default:
		return Layout{}, fmt.Errorf("unsupported OS for service install: %s", goos)
	}
}
