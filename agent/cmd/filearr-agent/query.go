package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	cli "github.com/urfave/cli/v3"

	"github.com/filearr/filearr/agent/internal/clicore"
	"github.com/filearr/filearr/agent/internal/localapi"
)

// queryDescription is the `filearr query --help` prose. It documents the two
// things the brief requires callers understand: the local typo-tolerance gap
// (§4.3) and the offline guarantee.
const queryDescription = `Query this machine's agent over the local socket/pipe (the SUPPORTED offline
query surface). The query string is the local filter DSL, e.g.:

  filearr query 'kind:video size:>1G modified:<7d'
  filearr query --json 'ext:pdf;doc' | jq -r .rel_path

Offline guarantee: the request rides a same-user Unix socket / Windows named pipe
— never a network socket — so this answers "where did I put that file" with the
central server fully unreachable.

Typo tolerance is LIMITED: the local index does bounded trigram + edit-distance
matching (fires on zero exact hits or an explicit ~term), which is NOT central
Meilisearch's fuzzy ranking. A zero-result local search does not prove the file
is absent — re-check against the central UI. Sidecar rows (.nfo/-thumb/…) are
excluded server-side and cannot be requested here.

Query history: your successful queries are ranked locally (zoxide-style frecency)
so repeated searches surface first. List them with:

  filearr query --history

Search history stays on this machine and is never sent to the central server.`

// queryCommand is the P7-T3 deliverable: it dials the P7-T2 transport (never the
// index file directly — that is `search`'s legacy path). Native urfave flags so
// shell completion covers them.
func queryCommand() *cli.Command {
	return &cli.Command{
		Name:            "query",
		Usage:           "Query the local index over the agent socket/pipe (supported offline surface)",
		ArgsUsage:       "<dsl query>",
		Description:     queryDescription,
		HideHelpCommand: true,
		Flags: []cli.Flag{
			&cli.StringFlag{Name: "data", Sources: cli.EnvVars(envDataDir), Value: defaultDataDir(), Usage: "data directory (used to locate the default socket/pipe)"},
			&cli.StringFlag{Name: "socket", Usage: "override the agent socket path (unix) / named-pipe name (windows); default is per-user"},
			&cli.BoolFlag{Name: "json", Usage: "emit NDJSON — one ResultRow per line, jq-parseable"},
			&cli.IntFlag{Name: "limit", Value: 50, Usage: "maximum rows to return"},
			&cli.IntFlag{Name: "offset", Value: 0, Usage: "row offset for pagination"},
			&cli.BoolFlag{Name: "plain", Usage: "disable ANSI color (plain columns); auto-off when output is not a TTY"},
			&cli.BoolFlag{Name: "verbose", Usage: "in a restricted view, list the full scope predicate set"},
			&cli.BoolFlag{Name: "history", Usage: "list your top local query history (frecency-ranked); this data never leaves the machine"},
		},
		Action: runQuery,
	}
}

// runQuery executes the query subcommand against the local transport.
func runQuery(ctx context.Context, cmd *cli.Command) error {
	out := writerOr(cmd.Root().Writer, os.Stdout)
	errOut := writerOr(cmd.Root().ErrWriter, os.Stderr)

	path := cmd.String("socket")
	if path == "" {
		path = localapi.DefaultPath(cmd.String("data"))
	}

	// --history lists local frecency suggestions instead of running a search; it
	// takes no query argument.
	if cmd.Bool("history") {
		return runHistory(ctx, cmd, out, path)
	}

	raw := strings.TrimSpace(strings.Join(cmd.Args().Slice(), " "))
	if raw == "" {
		return fmt.Errorf("usage: filearr-agent query [--json] [--limit N] [--offset N] [--plain] [--verbose] <query>  (or: --history)")
	}

	client := clicore.Dial(path)
	resp, err := client.Query(ctx, localapi.QueryRequest{
		Query:  raw,
		Limit:  cmd.Int("limit"),
		Offset: cmd.Int("offset"),
	})
	if err != nil {
		return queryError(err, path)
	}

	ropts := clicore.RenderOptions{
		Verbose: cmd.Bool("verbose"),
		Offset:  cmd.Int("offset"),
		Limit:   cmd.Int("limit"),
	}
	if cmd.Bool("json") {
		if err := clicore.RenderJSON(out, resp); err != nil {
			return err
		}
		clicore.RenderFooter(errOut, resp, ropts)
		return nil
	}

	// Color only on a real TTY, with --plain not given and NO_COLOR unset. Piped
	// output is thus always ANSI-free even without --plain.
	ropts.Color = clicore.ColorEnabled(clicore.IsTerminal(out), cmd.Bool("plain"))
	if err := clicore.Render(out, resp, ropts); err != nil {
		return err
	}
	clicore.RenderFooter(errOut, resp, ropts)
	return nil
}

// runHistory lists the local frecency-ranked query history (P7-T6). The data is
// strictly local — served over the same same-user socket/pipe, never from central.
func runHistory(ctx context.Context, cmd *cli.Command, out io.Writer, path string) error {
	client := clicore.Dial(path)
	resp, err := client.History(ctx, cmd.Int("limit"))
	if err != nil {
		return queryError(err, path)
	}
	if cmd.Bool("json") {
		return clicore.RenderHistoryJSON(out, resp)
	}
	if len(resp.Entries) == 0 {
		fmt.Fprintln(out, "no local query history yet")
		return nil
	}
	ropts := clicore.RenderOptions{Color: clicore.ColorEnabled(clicore.IsTerminal(out), cmd.Bool("plain"))}
	return clicore.RenderHistory(out, resp, ropts)
}

// queryError turns a transport/parse/exec failure into an actionable message.
func queryError(err error, path string) error {
	var te *clicore.TransportError
	var qe *clicore.QueryError
	switch {
	case errors.As(err, &te):
		return fmt.Errorf("cannot reach the local agent at %s: %v\n  is the agent daemon running? start it with: filearr-agent run", path, te.Err)
	case errors.As(err, &qe):
		switch qe.Code {
		case "unsupported_filter":
			return fmt.Errorf("query not runnable on the local index [%s]: %s\n  unsupported key(s): %s\n  tag/meta./cf. filters need central search — this machine's index carries only the R1 field set",
				qe.Code, qe.Message, strings.Join(qe.Keys, ", "))
		default:
			if qe.Position != nil {
				return fmt.Errorf("query syntax error [%s] at position %d: %s", qe.Code, *qe.Position, firstNonEmpty(qe.Reason, qe.Message))
			}
			return fmt.Errorf("query failed [%s]: %s", qe.Code, qe.Message)
		}
	default:
		return err
	}
}

// writerOr returns w, or fallback when w is nil (urfave leaves Writer/ErrWriter
// nil until setup; tests may inject buffers).
func writerOr(w io.Writer, fallback io.Writer) io.Writer {
	if w == nil {
		return fallback
	}
	return w
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}
