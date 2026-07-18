//go:build linux

package localapi

import (
	"fmt"
	"net"
	"os"

	"golang.org/x/sys/unix"
)

// checkPeerConn enforces the same-OS-user invariant on Linux via SO_PEERCRED — the
// kernel snapshots the connecting process's {pid,uid,gid} at connect() time, so a
// client cannot spoof it (the systemd sd-bus / Postgres `peer` model, brief §3.3).
// Any UID other than the agent's own is rejected.
func checkPeerConn(conn net.Conn) error {
	uc, ok := conn.(*net.UnixConn)
	if !ok {
		return fmt.Errorf("peer check: not a unix connection (%T)", conn)
	}
	raw, err := uc.SyscallConn()
	if err != nil {
		return fmt.Errorf("peer check: raw conn: %w", err)
	}
	var cred *unix.Ucred
	var credErr error
	if cerr := raw.Control(func(fd uintptr) {
		cred, credErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	}); cerr != nil {
		return fmt.Errorf("peer check: control: %w", cerr)
	}
	if credErr != nil {
		return fmt.Errorf("peer check: SO_PEERCRED: %w", credErr)
	}
	if !sameUID(cred.Uid) {
		return fmt.Errorf("peer uid %d != agent uid %d", cred.Uid, os.Getuid())
	}
	return nil
}

// sameUID is the pure comparison seam (unit-testable without a real socket): the
// peer UID must equal the agent process's own UID.
func sameUID(peer uint32) bool {
	return peer == uint32(os.Getuid())
}
