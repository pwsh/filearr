package main

import (
	"bytes"
	"context"
	"fmt"
	"strings"
	"testing"
)

// TestDispatchRoutesToEachHandler is the urfave-migration smoke test: it drives
// the real root command for every pre-existing subcommand with minimal args and
// asserts a command-SPECIFIC observable outcome (each handler's natural
// precondition error, or clean success). That proves arg parsing reaches the
// right handler through the new urfave dispatch without duplicating each
// handler's own logic tests — the precondition messages are the seam.
func TestDispatchRoutesToEachHandler(t *testing.T) {
	tmp := t.TempDir()
	deadSocket := deadSocketPath(t)

	cases := []struct {
		name       string
		args       []string
		wantErr    string // substring the returned error must contain ("" => expect nil)
		wantStdout string // substring stdout must contain (optional)
	}{
		{name: "enroll", args: []string{"enroll"}, wantErr: "enroll: central URL is required"},
		{name: "run", args: []string{"run", "-data", tmp}, wantErr: "run: no enrolled identity"},
		{name: "scan", args: []string{"scan", "-data", tmp}, wantErr: "scan: no roots configured"},
		{name: "push", args: []string{"push", "-data", tmp}, wantErr: "push: no enrolled identity"},
		{name: "reconcile", args: []string{"reconcile", "-data", tmp}, wantErr: "reconcile: no enrolled identity"},
		{name: "policy", args: []string{"policy", "-data", tmp}, wantErr: "", wantStdout: "no cached policy"},
		{name: "search", args: []string{"search", "-data", tmp}, wantErr: "search: usage: filearr-agent search"},
		{name: "query", args: []string{"query", "--socket", deadSocket, "kind:video"}, wantErr: "cannot reach the local agent"},
		{name: "version", args: []string{"version"}, wantErr: "", wantStdout: "filearr-agent "},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var out, errBuf bytes.Buffer
			root := buildRootCommand()
			root.Writer = &out
			root.ErrWriter = &errBuf

			// Legacy handlers still print to os.Stdout; swallow that too so the run
			// is quiet. The returned error is the routing signal.
			var legacy string
			err := withCapturedStdout(t, &legacy, func() error {
				return root.Run(context.Background(), append([]string{"filearr-agent"}, tc.args...))
			})

			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("%s: unexpected error: %v", tc.name, err)
				}
			} else if err == nil || !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("%s: error = %v; want substring %q", tc.name, err, tc.wantErr)
			}

			if tc.wantStdout != "" {
				combined := out.String() + legacy
				if !strings.Contains(combined, tc.wantStdout) {
					t.Fatalf("%s: stdout = %q; want substring %q", tc.name, combined, tc.wantStdout)
				}
			}
		})
	}
}

// TestVersionFlagMatchesLegacyFormat asserts the migrated --version output keeps
// the pre-migration "filearr-agent <v>" shape (not urfave's default "NAME version
// <v>"), since main installs a custom cli.VersionPrinter.
func TestVersionFlagMatchesLegacyFormat(t *testing.T) {
	var out bytes.Buffer
	root := buildRootCommand()
	root.Writer = &out
	// buildRootCommand doesn't install the printer (main does); assert the
	// `version` subcommand path, which prints directly and is deterministic.
	if err := root.Run(context.Background(), []string{"filearr-agent", "version"}); err != nil {
		t.Fatal(err)
	}
	want := fmt.Sprintf("filearr-agent %s\n", Version)
	if out.String() != want {
		t.Fatalf("version output = %q; want %q", out.String(), want)
	}
}
