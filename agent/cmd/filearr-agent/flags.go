package main

import (
	"flag"
	"log/slog"
	"os"
)

// newFlagSet returns a flag set that reports parse errors to stderr and does
// not os.Exit on -h (so the caller controls the exit code).
func newFlagSet(name string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	return fs
}

// bindCommonFlags registers the flags shared by enroll and run, seeding each
// default from its FILEARR_AGENT_* environment fallback.
func bindCommonFlags(fs *flag.FlagSet) *config {
	cfg := &config{}
	fs.StringVar(&cfg.CentralURL, "central", envOr(envCentralURL, ""), "central Filearr base URL (e.g. https://filearr.example.com)")
	fs.StringVar(&cfg.DataDir, "data", envOr(envDataDir, defaultDataDir()), "data directory for key/cert/state")
	fs.StringVar(&cfg.Name, "name", envOr(envName, ""), "friendly agent name (default: this device's hostname)")
	return cfg
}

// newLogger returns a text slog logger at info level on stderr.
func newLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
}
