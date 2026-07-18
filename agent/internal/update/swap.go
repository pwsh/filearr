package update

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// A/B binary swap per OS (research §5.2). The mechanics differ slightly by OS
// but the algorithm is uniform: rename the running binary aside (keeping it as
// the rollback source), move the new binary into the original path, then re-exec.
//
//   - Linux/macOS: rename(2) keeps the running process's open inode valid, so
//     the live process keeps executing while new launches resolve the new path.
//   - Windows: a running .exe cannot be deleted/overwritten but CAN be renamed
//     aside (rclone/Tailscale selfupdate pattern), which frees the original
//     path for the new binary.
//   - macOS BUNDLE caveat: we ship a BARE binary, not a signed .app bundle, so
//     the Sparkle "replace the whole signed bundle atomically, never patch a
//     file inside it" rule (Apple TN3126) does NOT apply here. IF Filearr ever
//     ships a notarized .app, this swap must be replaced with a whole-bundle
//     swap — flagged, not silently assumed away.

// PreviousBinaryPath is the rollback path the running binary is renamed to
// before a swap. Exposed so the boot-check can restore it.
func PreviousBinaryPath(current string) string {
	if runtime.GOOS == "windows" {
		return strings.TrimSuffix(current, ".exe") + ".old.exe"
	}
	return current + ".old"
}

// Apply performs the A/B swap: it renames “current“ aside to its
// PreviousBinaryPath and moves “newBinary“ into “current“. It returns the
// rollback path (the renamed-aside old binary) for the caller to record in the
// update state. On any failure it best-effort restores the original binary.
//
// Apply does NOT re-exec — the caller writes the boot-counter state (with the
// returned previous path) and then calls ReExec + exits, so a crash between the
// swap and the re-exec still leaves a recoverable state file on disk.
func Apply(newBinary, current string) (previous string, err error) {
	previous = PreviousBinaryPath(current)
	_ = os.Remove(previous) // drop any stale rollback file from a prior update
	if err := os.Rename(current, previous); err != nil {
		return "", fmt.Errorf("rename running binary aside: %w", err)
	}
	if err := moveInto(newBinary, current); err != nil {
		_ = os.Rename(previous, current) // restore — the swap did not complete
		return "", err
	}
	return previous, nil
}

// Restore puts “previous“ back at “current“ during a rollback. Because the
// broken new binary may itself be running (Windows cannot delete a running exe),
// it renames the broken binary aside first, then moves the previous binary into
// place. The broken file is best-effort removed (it may linger on Windows until
// the next boot, which is harmless).
func Restore(previous, current string) error {
	broken := current + ".broken"
	_ = os.Remove(broken)
	if err := os.Rename(current, broken); err != nil {
		return fmt.Errorf("rename broken binary aside: %w", err)
	}
	if err := moveInto(previous, current); err != nil {
		_ = os.Rename(broken, current)
		return err
	}
	_ = os.Remove(broken)
	return nil
}

// ReExec launches path with argv (inheriting stdio + env) and returns. The
// caller then exits so the service supervisor observes a clean handoff; the
// freshly-spawned process is released (not waited on).
func ReExec(path string, argv []string) error {
	proc, err := os.StartProcess(path, argv, &os.ProcAttr{
		Files: []*os.File{os.Stdin, os.Stdout, os.Stderr},
		Env:   os.Environ(),
	})
	if err != nil {
		return fmt.Errorf("re-exec %s: %w", path, err)
	}
	return proc.Release()
}

// moveInto moves src to dst, preferring a same-volume rename and falling back to
// a copy (cross-volume: the download temp dir and the install path may be on
// different filesystems). On non-Windows it ensures the destination is
// executable.
func moveInto(src, dst string) error {
	if err := os.Rename(src, dst); err == nil {
		return ensureExecutable(dst)
	}
	if err := copyFile(src, dst); err != nil {
		return err
	}
	_ = os.Remove(src)
	return ensureExecutable(dst)
}

func ensureExecutable(path string) error {
	if runtime.GOOS == "windows" {
		return nil
	}
	if err := os.Chmod(path, 0o755); err != nil {
		return fmt.Errorf("chmod %s: %w", path, err)
	}
	return nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return fmt.Errorf("open new binary: %w", err)
	}
	defer in.Close()
	tmp := dst + ".new"
	out, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return fmt.Errorf("create %s: %w", tmp, err)
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("copy new binary: %w", err)
	}
	if err := out.Sync(); err != nil {
		out.Close()
		_ = os.Remove(tmp)
		return fmt.Errorf("sync %s: %w", tmp, err)
	}
	if err := out.Close(); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return os.Rename(tmp, dst)
}

// atomicWrite writes data to a temp file in the same directory then renames over
// the target (shared by SaveState + the download path).
func atomicWrite(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmpName, perm); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}
