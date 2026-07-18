// Command filearr-agent is the per-machine companion to a central Filearr server.
// It performs enrollment + certificate lifecycle (enroll/run), local scanning +
// replication (scan/push/reconcile), policy inspection (policy), and local
// read-only query (search/query).
//
// Dispatch framework — urfave/cli v3 (P7-T3 Architect ruling). The earlier
// stdlib-flag `switch cmd` in main() was an explicit placeholder pending the CLI
// framework decision; P7-T3 chose urfave/cli v3 (MIT, low-dependency), so the
// top-level dispatch, `--help`/`--version`, unknown-command handling, and shell
// completion now come from it. The migration is deliberately mechanical: every
// pre-existing subcommand keeps its byte-compatible stdlib-flag parsing by running
// under SkipFlagParsing and delegating to its original run* handler — only the
// NEW `query` command defines native urfave flags (so completion covers them).
//
// User-facing branding: the verb `filearr query` is this binary's `query`
// subcommand. Packaging a `filearr` symlink/alias → `filearr-agent` gives the
// branded single-binary UX (see agent/README.md).
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	cli "github.com/urfave/cli/v3"
)

// Version is the agent build version, overridable at link time via
// -ldflags "-X main.Version=vX.Y.Z".
var Version = "0.0.0-dev"

func main() {
	// Match the pre-migration `version` output ("filearr-agent <v>") rather than
	// urfave's default "NAME version <v>".
	cli.VersionPrinter = func(cmd *cli.Command) {
		fmt.Fprintf(writerOr(cmd.Root().Writer, os.Stdout), "filearr-agent %s\n", Version)
	}

	ctx, cancel := signalContext()
	defer cancel()

	root := buildRootCommand()
	if err := root.Run(ctx, os.Args); err != nil {
		// urfave prints only ExitCoder/usage errors itself; a plain handler error
		// bubbles here. Preserve the pre-migration "filearr-agent <cmd>: <err>"
		// shape (the legacy adapter already prefixes the command name).
		fmt.Fprintf(os.Stderr, "filearr-agent %v\n", err)
		os.Exit(1)
	}
}

// buildRootCommand assembles the urfave/cli v3 command tree. Exposed (unexported
// but package-visible) so tests can drive dispatch without spawning a process.
func buildRootCommand() *cli.Command {
	return &cli.Command{
		Name:                  "filearr-agent",
		Usage:                 "Filearr distributed agent — enrollment, scanning, replication, and local query",
		Version:               Version,
		EnableShellCompletion: true, // bash/zsh/fish/pwsh completion of command + query flag names
		HideHelpCommand:       true,
		Description: `Typical lifecycle: enroll once, scan your folders, then leave 'run' going as a
service. Everything else is one-shot maintenance or local search.

EXAMPLES:
   Enroll this machine (mint the single-use token in Admin -> Agents):
     filearr-agent enroll -central https://filearr.example.com -token fae_...
     (the agent name defaults to this device's hostname; override with -name)

   Index folders into the local catalog (repeatable; remembered for the daemon):
     filearr-agent scan -root /mnt/media -root /mnt/photos
     filearr-agent scan -root /mnt/media -watch      keep watching for changes

   Run the background daemon (replication, verification, policy, local query
   API, web UI, self-update) — wrap this in a service for real installs:
     filearr-agent run

   Search the local index (works fully offline; results are typo-tolerant):
     filearr-agent query 'kind:video size:>1G arcane'
     filearr-agent query -json 'arcane' | jq .rel_path

   One-shot maintenance:
     filearr-agent push             drain pending changes to central now
     filearr-agent reconcile        full-manifest consistency sweep
     filearr-agent policy -fetch    pull + apply the central policy now
     filearr-agent update -check    is a signed update available?

   Every command also reads FILEARR_AGENT_* environment variables in place of
   flags (FILEARR_AGENT_CENTRAL_URL, FILEARR_AGENT_TOKEN, FILEARR_AGENT_DATA_DIR,
   FILEARR_AGENT_NAME); run 'filearr-agent <command> -h' for per-command flags.`,
		// Root Action fires only when no subcommand matched: with leftover args that
		// is an unknown command (error, non-zero exit — urfave would otherwise print
		// "No help topic" and exit 0); with none, show help.
		Action: func(ctx context.Context, cmd *cli.Command) error {
			if cmd.Args().Len() > 0 {
				return fmt.Errorf("unknown subcommand %q (run `filearr-agent --help`)", cmd.Args().First())
			}
			return cli.ShowAppHelp(cmd)
		},
		Commands: []*cli.Command{
			legacyCommand("enroll", "Register with central, obtain a client cert from step-ca, and bind it", runEnroll),
			legacyCommand("run", "Load the cert store and run the renewal + replication + policy + local-query daemon", runDaemon),
			legacyCommand("scan", "Walk one or more roots into the local SQLite/FTS5 index [--watch]", runScan),
			legacyCommand("push", "Drain the replication outbox to central once (until empty or error)", runPush),
			legacyCommand("reconcile", "Full-manifest reconciliation sweep of all roots (one-shot)", runReconcile),
			legacyCommand("policy", "Print the cached central policy, or --fetch a one-shot poll+apply", runPolicy),
			legacyCommand("update", "Check for a signed agent update and apply it [--check to only report]", runUpdate),
			legacyCommand("search", "Query the local index by opening the index file directly (legacy debug path; prefer `query`)", runSearch),
			queryCommand(),
			versionCommand(),
		},
	}
}

// versionCommand preserves the pre-migration `version` subcommand. It prints
// directly (not via cli.VersionPrinter) so the output is identical whether main
// has installed the custom printer — matching the old "filearr-agent <v>" line.
func versionCommand() *cli.Command {
	return &cli.Command{
		Name:            "version",
		Usage:           "Print the agent version",
		HideHelpCommand: true,
		Action: func(_ context.Context, cmd *cli.Command) error {
			fmt.Fprintf(writerOr(cmd.Root().Writer, os.Stdout), "filearr-agent %s\n", Version)
			return nil
		},
	}
}

// legacyCommand wraps a pre-migration stdlib-flag handler (run*(args []string))
// as a urfave command. SkipFlagParsing hands every token after the command name
// through to the original flag.FlagSet, so flag/env/behavior stay byte-compatible;
// urfave only owns dispatch, help listing, and the command name. The returned
// error is prefixed with the command name to preserve the old message shape.
func legacyCommand(name, usage string, run func(args []string) error) *cli.Command {
	return &cli.Command{
		Name:            name,
		Usage:           usage,
		SkipFlagParsing: true,
		HideHelpCommand: true,
		Action: func(_ context.Context, cmd *cli.Command) error {
			if err := run(cmd.Args().Slice()); err != nil {
				return fmt.Errorf("%s: %w", name, err)
			}
			return nil
		},
	}
}

// signalContext returns a context cancelled on SIGINT/SIGTERM for graceful
// shutdown.
func signalContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}
