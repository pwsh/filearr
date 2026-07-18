//go:build windows

package clicore

import (
	"context"
	"net"

	winio "github.com/Microsoft/go-winio"
)

// platformDial returns an http.Transport DialContext that connects to the agent's
// named pipe. The pipe DACL restricts it to the current user's SID (P7-T2), so a
// dial only succeeds for the SAME OS user — this is the client side of the
// peer-credential model, and a different user is denied at connect() by the
// kernel before any request is sent.
func platformDial(path string) func(ctx context.Context, network, addr string) (net.Conn, error) {
	return func(ctx context.Context, _, _ string) (net.Conn, error) {
		return winio.DialPipeContext(ctx, path)
	}
}
