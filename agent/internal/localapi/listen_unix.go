//go:build linux || darwin

package localapi

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

// DefaultPath returns the default Unix-socket path under the per-user data dir
// (<DataDir>/agent.sock). DataDir is already a per-user location (os.UserConfigDir)
// with 0700 perms, so there is no cross-user name collision.
func DefaultPath(dataDir string) string {
	return filepath.Join(dataDir, "agent.sock")
}

// platformListen creates the local Unix-domain-socket listener at path. It sets
// umask 0077 BEFORE net.Listen (no race window — golang/go#11822: Listen("unix")
// has no mode parameter), safely unlinks a stale socket, and asserts the bound
// socket is not group/other-accessible immediately after listen.
func platformListen(path string) (net.Listener, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("create data dir for socket: %w", err)
	}
	if err := clearStaleSocket(path); err != nil {
		return nil, err
	}

	// Pre-listen umask so the socket is created 0700-class with no world window.
	old := syscall.Umask(0o077)
	ln, err := net.Listen("unix", path)
	syscall.Umask(old)
	if err != nil {
		return nil, fmt.Errorf("listen unix %s: %w", path, err)
	}

	// Tighten to 0600 (owner rw is all a peer needs to connect) and assert no
	// group/other bits leaked — fail closed if the OS handed back a wider mode.
	if err := os.Chmod(path, 0o600); err != nil {
		ln.Close()
		return nil, fmt.Errorf("chmod socket: %w", err)
	}
	fi, err := os.Stat(path)
	if err != nil {
		ln.Close()
		return nil, fmt.Errorf("stat socket: %w", err)
	}
	if fi.Mode().Perm()&0o077 != 0 {
		ln.Close()
		return nil, fmt.Errorf("socket %s has world/group-accessible mode %o; refusing", path, fi.Mode().Perm())
	}
	return ln, nil
}

// clearStaleSocket removes a leftover socket file at path, but ONLY when no live
// agent is listening on it: if a dial succeeds another instance owns it and we
// refuse (single-instance). A dial failure means the socket is stale → unlink it.
func clearStaleSocket(path string) error {
	fi, err := os.Lstat(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return fmt.Errorf("stat existing socket: %w", err)
	}
	if fi.Mode()&os.ModeSocket == 0 {
		return fmt.Errorf("refusing to bind: %s exists and is not a socket", path)
	}
	if c, derr := net.DialTimeout("unix", path, 200*time.Millisecond); derr == nil {
		c.Close()
		return fmt.Errorf("another agent is already listening at %s", path)
	}
	if err := os.Remove(path); err != nil {
		return fmt.Errorf("remove stale socket %s: %w", path, err)
	}
	return nil
}
