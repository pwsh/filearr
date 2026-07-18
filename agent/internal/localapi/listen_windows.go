//go:build windows

package localapi

import (
	"fmt"
	"net"
	"sync"

	winio "github.com/Microsoft/go-winio"
	"golang.org/x/sys/windows"
)

// DefaultPath returns the default named-pipe path for the current user. The pipe
// name is scoped by the user's SID so two OS users on one machine never collide
// (the CLI derives the same name from its own — same-user — SID). dataDir is
// unused on Windows (the pipe lives in the kernel object namespace, not the FS);
// it is accepted for signature parity with the unix build.
func DefaultPath(dataDir string) string {
	_ = dataDir
	sid, err := currentUserSID()
	if err != nil || sid == "" {
		// Fail safe to a fixed name; the SDDL below still restricts access to the
		// current user, so a name clash between users is denied at the ACL layer.
		return `\\.\pipe\filearr-agent`
	}
	return `\\.\pipe\filearr-agent-` + sid
}

// platformListen creates the local named-pipe listener at path with a security
// descriptor that restricts the pipe DACL to the CURRENT USER's SID only.
//
// R5 — VERIFIED SDDL (native Windows, this implementation):
//
//	D:P(A;;GA;;;<current-user-SID>)
//
// where:
//   - D:            the security descriptor's DACL
//   - P             SDDL_PROTECTED — blocks inherited ACEs, so no parent-object
//     ACE can widen access
//   - (A;;GA;;;SID) an ALLOW ace granting GENERIC_ALL (GA) to the user's SID
//
// No ACE grants World/Everyone (SDDL "WD" / S-1-1-0), Authenticated Users ("AU"),
// or any group — only the caller's own SID appears, so a different OS user is
// denied at connect() by the kernel before the peer check even runs. The SID is
// derived at runtime from the process token (never hardcoded). Owner/group are
// left to the creator default (the current user).
func platformListen(path string) (net.Listener, error) {
	sddl, err := currentUserSDDL()
	if err != nil {
		return nil, fmt.Errorf("build pipe security descriptor: %w", err)
	}
	ln, err := winio.ListenPipe(path, &winio.PipeConfig{SecurityDescriptor: sddl})
	if err != nil {
		return nil, fmt.Errorf("listen pipe %s: %w", path, err)
	}
	return ln, nil
}

// checkPeerConn enforces the same-OS-user invariant on Windows as a second layer
// beneath the SDDL: it reads the connecting client's PID via
// GetNamedPipeClientProcessId, opens that process token, and compares its user SID
// to the agent's own (brief §3.3). Any other SID is rejected.
func checkPeerConn(conn net.Conn) error {
	fd, ok := conn.(interface{ Fd() uintptr })
	if !ok {
		return fmt.Errorf("peer check: connection has no pipe handle (%T)", conn)
	}
	h := windows.Handle(fd.Fd())

	var pid uint32
	if err := windows.GetNamedPipeClientProcessId(h, &pid); err != nil {
		return fmt.Errorf("peer check: GetNamedPipeClientProcessId: %w", err)
	}
	proc, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		return fmt.Errorf("peer check: OpenProcess(pid=%d): %w", pid, err)
	}
	defer windows.CloseHandle(proc)

	var token windows.Token
	if err := windows.OpenProcessToken(proc, windows.TOKEN_QUERY, &token); err != nil {
		return fmt.Errorf("peer check: OpenProcessToken: %w", err)
	}
	defer token.Close()

	tu, err := token.GetTokenUser()
	if err != nil {
		return fmt.Errorf("peer check: GetTokenUser: %w", err)
	}
	self, err := currentUserSID()
	if err != nil {
		return fmt.Errorf("peer check: self SID: %w", err)
	}
	if !sidEqual(tu.User.Sid, self) {
		return fmt.Errorf("peer sid %s != agent sid %s", tu.User.Sid.String(), self)
	}
	return nil
}

// sidEqual is the pure comparison seam (unit-testable with a fabricated SID): it
// reports whether sid's string form equals the agent's own user SID string.
func sidEqual(sid *windows.SID, selfSIDString string) bool {
	return sid != nil && selfSIDString != "" && sid.String() == selfSIDString
}

var (
	selfSIDOnce sync.Once
	selfSIDStr  string
	selfSIDErr  error
)

// currentUserSID returns the agent process's own user SID as an SDDL string,
// computed once from the process token.
func currentUserSID() (string, error) {
	selfSIDOnce.Do(func() {
		token, err := windows.OpenCurrentProcessToken()
		if err != nil {
			selfSIDErr = err
			return
		}
		defer token.Close()
		tu, err := token.GetTokenUser()
		if err != nil {
			selfSIDErr = err
			return
		}
		selfSIDStr = tu.User.Sid.String()
	})
	return selfSIDStr, selfSIDErr
}

// currentUserSDDL builds the current-user-only pipe security descriptor. See
// platformListen's doc comment for the verified string form.
func currentUserSDDL() (string, error) {
	sid, err := currentUserSID()
	if err != nil {
		return "", err
	}
	if sid == "" {
		return "", fmt.Errorf("empty current-user SID")
	}
	return "D:P(A;;GA;;;" + sid + ")", nil
}
