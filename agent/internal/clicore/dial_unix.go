//go:build linux || darwin

package clicore

import (
	"context"
	"net"
)

// platformDial returns an http.Transport DialContext that connects to the agent's
// Unix domain socket. The socket lives under the per-user 0700 data dir with 0600
// perms (P7-T2), so only the owning OS user can reach it — the client side of the
// peer-credential model.
func platformDial(path string) func(ctx context.Context, network, addr string) (net.Conn, error) {
	d := &net.Dialer{}
	return func(ctx context.Context, _, _ string) (net.Conn, error) {
		return d.DialContext(ctx, "unix", path)
	}
}
