package main

import (
	"flag"
	"os"

	"github.com/filearr/filearr/agent/internal/sidecar"
)

// newFlagSet returns a flag set that reports parse errors to stderr and does
// not os.Exit on -h (so the caller controls the exit code).
func newFlagSet(name string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	return fs
}

// bindCommonFlags registers the flags shared by enroll and run, seeding each
// default from the precedence chain env > sidecar > built-in default (an
// explicit flag, parsed after this, is the highest-precedence override). The
// sidecar was already resolved by setupRuntime; activeSidecar() returns it (or
// an empty config).
//
// The -config / -log-level / -log-dir flags are registered here so every command
// that shares this set ACCEPTS and documents them; their runtime effect (sidecar
// discovery + logger configuration) is applied earlier in setupRuntime, using
// the same precedence, so the parsed values here are informational.
func bindCommonFlags(fs *flag.FlagSet) *config {
	cfg := &config{}
	sc := activeSidecar()
	fs.StringVar(&cfg.CentralURL, "central", envOr(envCentralURL, sc.CentralURL), "central Filearr base URL (e.g. https://filearr.example.com)")
	fs.StringVar(&cfg.DataDir, "data", envOr(envDataDir, firstNonEmpty(sc.DataDir, defaultDataDir())), "data directory for key/cert/state")
	fs.StringVar(&cfg.Name, "name", envOr(envName, sc.AgentName), "friendly agent name (default: this device's hostname)")
	fs.StringVar(&cfg.ConfigPath, "config", envOr(sidecar.EnvConfigPath, sc.Path), "path to the filearr-agent.json sidecar config")
	fs.StringVar(&cfg.LogLevel, "log-level", envOr(envLogLevel, firstNonEmpty(sc.LogLevel, "info")), "log verbosity: error|warn|info|verbose|debug")
	fs.StringVar(&cfg.LogDir, "log-dir", envOr(envLogDir, sc.LogDir), "directory for the rotating filearr-agent.log (also echoes to stderr on a tty)")
	return cfg
}
