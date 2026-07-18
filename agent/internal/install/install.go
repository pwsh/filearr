package install

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
)

// ErrNeedAdmin is returned when install/uninstall is attempted without the
// elevation the service registration requires.
var ErrNeedAdmin = errors.New("administrator/root privileges are required (re-run elevated: Windows 'Run as administrator', Linux/macOS 'sudo')")

// Status is the service manager's view of the service, normalised across OSes.
type Status int

const (
	StatusUnknown Status = iota
	StatusRunning
	StatusStopped
	StatusNotInstalled
)

// Controller is the thin service-manager surface the installer drives. The real
// implementation wraps kardianos/service; tests inject a mock so no service is
// actually registered during unit tests.
type Controller interface {
	Install() error
	Uninstall() error
	Start() error
	Stop() error
	Restart() error
	Status() (Status, error)
}

// FS abstracts the filesystem side effects so install/uninstall decisions are
// testable against an in-memory fake.
type FS interface {
	MkdirAll(path string, perm os.FileMode) error
	CopyFile(src, dst string, perm os.FileMode) error
	Remove(path string) error
	RemoveAll(path string) error
	// SameFile reports whether src and dst resolve to the same on-disk file, so a
	// re-install whose source binary already IS the installed binary skips the
	// self-overwrite.
	SameFile(src, dst string) (bool, error)
}

// Installer performs the idempotent install / uninstall using injected effects.
type Installer struct {
	Layout    Layout
	SourceExe string // path to the running binary to copy into place
	FS        FS
	Service   Controller

	// IsAdmin reports whether the current process is elevated. Required.
	IsAdmin func() bool
	// Enrolled reports whether the agent already has an on-disk identity. When
	// nil the installer treats the agent as not-yet-enrolled.
	Enrolled func() bool
	// Enroll runs the non-interactive enroll flow. Called only when HasToken is
	// true and the agent is not already enrolled. Nil disables enrollment.
	Enroll   func() error
	HasToken bool

	Log *slog.Logger
}

func (in *Installer) log() *slog.Logger {
	if in.Log != nil {
		return in.Log
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// vlog emits a verbose-level record (the install seams the user asked to be
// able to see without the full debug firehose).
func (in *Installer) vlog(msg string, args ...any) {
	in.log().Log(context.Background(), verboseLevel, msg, args...)
}

// dirPerm returns the create mode for each install directory. Data holds private
// keys so it is owner-only on POSIX; the rest are world-readable dirs.
func (in *Installer) dirs() []struct {
	path string
	perm os.FileMode
} {
	return []struct {
		path string
		perm os.FileMode
	}{
		{in.Layout.InstallDir, 0o755},
		{in.Layout.DataDir, 0o700},
		{in.Layout.ConfigDir, 0o755},
		{in.Layout.LogDir, 0o755},
	}
}

// Install lays out the install tree, (re)places the binary, optionally enrolls,
// and registers + starts the service. It is idempotent: a re-run stops and
// deregisters any existing service first, then re-registers with the current
// configuration and starts it (in-place upgrade). Requires elevation.
func (in *Installer) Install() error {
	if in.IsAdmin == nil || !in.IsAdmin() {
		return ErrNeedAdmin
	}
	for _, d := range in.dirs() {
		if err := in.FS.MkdirAll(d.path, d.perm); err != nil {
			return fmt.Errorf("create %s: %w", d.path, err)
		}
	}
	in.vlog("install layout created", "install_dir", in.Layout.InstallDir, "data_dir", in.Layout.DataDir, "log_dir", in.Layout.LogDir)

	// (b) place the binary unless the source already IS the target (re-running
	// `install` from the installed path).
	same, _ := in.FS.SameFile(in.SourceExe, in.Layout.BinPath)
	if same {
		in.vlog("binary already in place; skipping copy", "path", in.Layout.BinPath)
	} else {
		if err := in.FS.CopyFile(in.SourceExe, in.Layout.BinPath, 0o755); err != nil {
			return fmt.Errorf("copy binary to %s: %w", in.Layout.BinPath, err)
		}
		in.vlog("binary copied", "from", in.SourceExe, "to", in.Layout.BinPath)
	}

	// (e) idempotency: if a service is already registered, stop + deregister so
	// the (re)Install applies the current config cleanly.
	if st, err := in.Service.Status(); err == nil && st != StatusNotInstalled {
		in.vlog("existing service found; stopping + deregistering for in-place upgrade", "status", st)
		_ = in.Service.Stop()
		_ = in.Service.Uninstall()
	}

	// (c) non-interactive enroll when a token is present and we are not enrolled,
	// BEFORE the service starts so it comes up already-enrolled.
	if in.HasToken && in.Enroll != nil {
		enrolled := in.Enrolled != nil && in.Enrolled()
		if enrolled {
			in.vlog("agent already enrolled; skipping enroll during install")
		} else {
			in.log().Info("enrolling agent during install")
			if err := in.Enroll(); err != nil {
				return fmt.Errorf("enroll during install: %w", err)
			}
		}
	}

	// (d) register + start.
	if err := in.Service.Install(); err != nil {
		return fmt.Errorf("register service: %w", err)
	}
	if err := in.Service.Start(); err != nil {
		return fmt.Errorf("start service: %w", err)
	}
	in.log().Info("service installed and started")
	return nil
}

// Uninstall stops + deregisters the service and removes the installed binary.
// When purge is false the data/config/log directories are KEPT and returned so
// the caller can report them; purge additionally removes them. Requires
// elevation.
func (in *Installer) Uninstall(purge bool) (kept []string, err error) {
	if in.IsAdmin == nil || !in.IsAdmin() {
		return nil, ErrNeedAdmin
	}
	if st, serr := in.Service.Status(); serr == nil && st != StatusNotInstalled {
		_ = in.Service.Stop()
		if err := in.Service.Uninstall(); err != nil {
			return nil, fmt.Errorf("deregister service: %w", err)
		}
		in.vlog("service stopped + deregistered")
	}
	if err := in.FS.Remove(in.Layout.BinPath); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("remove binary %s: %w", in.Layout.BinPath, err)
	}
	if purge {
		for _, d := range []string{in.Layout.DataDir, in.Layout.LogDir, in.Layout.ConfigDir} {
			if err := in.FS.RemoveAll(d); err != nil {
				return nil, fmt.Errorf("purge %s: %w", d, err)
			}
		}
		in.log().Info("service uninstalled; data/logs/config purged")
		return nil, nil
	}
	// Dedup ConfigDir==DataDir (Windows/macOS share one dir).
	kept = dedup([]string{in.Layout.DataDir, in.Layout.ConfigDir, in.Layout.LogDir})
	in.log().Info("service uninstalled; data/logs/config kept", "kept", kept)
	return kept, nil
}

func dedup(in []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, s := range in {
		if s == "" || seen[s] {
			continue
		}
		seen[s] = true
		out = append(out, s)
	}
	return out
}

// verboseLevel duplicates agentlog.LevelVerbose to keep this package free of a
// dependency on agentlog (which pulls lumberjack/term). Kept numerically in sync.
const verboseLevel = slog.Level(-2)
