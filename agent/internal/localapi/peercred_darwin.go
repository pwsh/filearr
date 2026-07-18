//go:build darwin

package localapi

import (
	"fmt"
	"net"
	"os"

	"golang.org/x/sys/unix"
)

// checkPeerConn enforces the same-OS-user invariant on macOS via
// getsockopt(LOCAL_PEERCRED) → xucred, whose Uid is the connecting process's
// effective UID snapshotted by the kernel (the BSD getpeereid equivalent,
// brief §3.3). Any UID other than the agent's own is rejected.
func checkPeerConn(conn net.Conn) error {
	uc, ok := conn.(*net.UnixConn)
	if !ok {
		return fmt.Errorf("peer check: not a unix connection (%T)", conn)
	}
	raw, err := uc.SyscallConn()
	if err != nil {
		return fmt.Errorf("peer check: raw conn: %w", err)
	}
	var xu *unix.Xucred
	var xErr error
	if cerr := raw.Control(func(fd uintptr) {
		xu, xErr = unix.GetsockoptXucred(int(fd), unix.SOL_LOCAL, unix.LOCAL_PEERCRED)
	}); cerr != nil {
		return fmt.Errorf("peer check: control: %w", cerr)
	}
	if xErr != nil {
		return fmt.Errorf("peer check: LOCAL_PEERCRED: %w", xErr)
	}
	if !sameUID(xu.Uid) {
		return fmt.Errorf("peer uid %d != agent uid %d", xu.Uid, os.Getuid())
	}
	return nil
}

// sameUID is the pure comparison seam (unit-testable without a real socket): the
// peer UID must equal the agent process's own UID.
func sameUID(peer uint32) bool {
	return peer == uint32(os.Getuid())
}
