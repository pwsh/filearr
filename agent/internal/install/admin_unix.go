//go:build !windows

package install

import "os"

// IsAdmin reports whether the process is running as root (euid 0), the
// precondition for registering a systemd/launchd unit and writing to the system
// install/config/log directories.
func IsAdmin() bool {
	return os.Geteuid() == 0
}
