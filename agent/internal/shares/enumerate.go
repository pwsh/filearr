package shares

import (
	"context"
	"os"
	"os/exec"
	"runtime"
	"time"
)

// enumerate.go is the ONLY OS-specific surface: it shells out / reads the
// well-known share-config files per platform and hands the raw text to the pure
// parsers in parse.go. It compiles on every target (a runtime.GOOS switch, no
// build tags, no cgo) — exec.Command and os.ReadFile are cross-platform in
// compilation; a command that does not exist on a given OS simply errors, which
// R1 swallows into "no exports".
//
// R1 (best-effort): every failure path returns whatever was gathered so far (or
// nil), NEVER an error. A permission-denied enumeration, an absent config file, a
// locked-down shell — all mean "no hint", not a broken scan.

// enumCmdTimeout bounds a share-enumeration subprocess so a wedged PowerShell /
// `sharing` invocation cannot stall a scan.
const enumCmdTimeout = 8 * time.Second

// enumerateOS dispatches to the per-OS discovery for the current platform.
func enumerateOS() []export {
	switch runtime.GOOS {
	case "windows":
		return enumerateWindows()
	case "linux":
		return enumerateLinux()
	case "darwin":
		return enumerateDarwin()
	default:
		return nil
	}
}

// enumerateWindows prefers PowerShell Get-SmbShare (CSV, robust quoting) and
// falls back to `net share` when PowerShell is unavailable or locked down.
func enumerateWindows() []export {
	if out, err := runCmd("powershell", "-NoProfile", "-NonInteractive", "-Command",
		"Get-SmbShare | Select-Object Name,Path | ConvertTo-Csv -NoTypeInformation"); err == nil {
		if ex := parseSmbShareCSV(out); len(ex) > 0 {
			return ex
		}
	}
	if out, err := runCmd("net", "share"); err == nil {
		return parseNetShare(out)
	}
	return nil
}

// enumerateLinux reads Samba's smb.conf (SMB shares) and /etc/exports (NFS). Both
// are optional; a host may run one, both, or neither.
func enumerateLinux() []export {
	var exports []export
	if b, err := os.ReadFile("/etc/samba/smb.conf"); err == nil {
		exports = append(exports, parseSmbConf(string(b))...)
	}
	if b, err := os.ReadFile("/etc/exports"); err == nil {
		exports = append(exports, parseExports(string(b))...)
	}
	return exports
}

// enumerateDarwin lists macOS share points via `sharing -l`.
func enumerateDarwin() []export {
	if out, err := runCmd("sharing", "-l"); err == nil {
		return parseSharingL(out)
	}
	return nil
}

// runCmd runs a discovery subprocess with a bounded timeout and returns its
// stdout. Any error (missing binary, non-zero exit, timeout) is returned for the
// caller to swallow per R1.
func runCmd(name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), enumCmdTimeout)
	defer cancel()
	out, err := exec.CommandContext(ctx, name, args...).Output()
	if err != nil {
		return "", err
	}
	return string(out), nil
}
