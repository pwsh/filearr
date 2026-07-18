// Package agentlog builds the agent's slog logger with a user-selectable level
// and optional rotating file output.
//
// Five user-facing level names map onto slog levels. slog is "lower value = more
// verbose", and a handler emits every record whose level is >= the handler's
// threshold, so the ordering below is deliberate:
//
//	name     slog.Level          shown when threshold <=
//	error    slog.LevelError (8)  error
//	warn     slog.LevelWarn  (4)  warn, error
//	info     slog.LevelInfo  (0)  info, warn, error            (default)
//	verbose  LevelVerbose   (-2)  verbose, info, warn, error
//	debug    slog.LevelDebug(-4)  everything
//
// "verbose" sits strictly between info and debug: it surfaces the extra
// operational seams (service lifecycle, sidecar resolution, install steps)
// without the full debug firehose. A handler set to info therefore hides verbose
// AND debug; a handler set to verbose shows verbose but still hides debug.
package agentlog

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/term"
	lumberjack "gopkg.in/natefinch/lumberjack.v2"
)

// LevelVerbose is the custom slog level between Info (0) and Debug (-4).
const LevelVerbose = slog.Level(-2)

// LogFileName is the fixed rotating log file name under a configured log dir.
const LogFileName = "filearr-agent.log"

// Rotation parameters (research: keep the on-disk footprint bounded on a small
// appliance while retaining enough history to diagnose a restart loop).
const (
	rotateMaxSizeMiB = 10 // lumberjack MaxSize is in MiB
	rotateMaxBackups = 5
	rotateCompress   = true
)

// ParseLevel maps a user-facing level name (case-insensitive) to its slog level.
// An empty string yields the info default with ok=true; an unrecognised name
// yields ok=false so the caller can report the bad value rather than silently
// defaulting.
func ParseLevel(name string) (slog.Level, bool) {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "":
		return slog.LevelInfo, true
	case "error":
		return slog.LevelError, true
	case "warn", "warning":
		return slog.LevelWarn, true
	case "info":
		return slog.LevelInfo, true
	case "verbose":
		return LevelVerbose, true
	case "debug":
		return slog.LevelDebug, true
	default:
		return slog.LevelInfo, false
	}
}

// Options configures New.
type Options struct {
	// Level is the resolved threshold (default slog.LevelInfo for the zero value).
	Level slog.Level
	// LogDir, when non-empty, enables the rotating file sink at
	// <LogDir>/filearr-agent.log. The directory is created if missing.
	LogDir string
	// Stderr forces stderr output. When a file sink is active, stderr is added
	// only if this is true AND stderr is a terminal (so a service run does not
	// duplicate every line into a captured stderr). When no file sink is active,
	// stderr is always used regardless of this flag.
	Stderr bool
}

// New builds a *slog.Logger and an io.Closer for any file sink (nil-safe to
// close; a no-op when only stderr is used). The custom VERBOSE level renders as
// "VERBOSE" in the text handler rather than slog's default "DEBUG+2".
func New(opts Options) (*slog.Logger, io.Closer, error) {
	var writers []io.Writer
	var closer io.Closer = noopCloser{}

	if opts.LogDir != "" {
		if err := os.MkdirAll(opts.LogDir, 0o755); err != nil {
			return nil, nil, fmt.Errorf("create log dir %s: %w", opts.LogDir, err)
		}
		lj := &lumberjack.Logger{
			Filename:   filepath.Join(opts.LogDir, LogFileName),
			MaxSize:    rotateMaxSizeMiB,
			MaxBackups: rotateMaxBackups,
			Compress:   rotateCompress,
		}
		writers = append(writers, lj)
		closer = lj
		// A tty attachment gets a live echo alongside the file.
		if opts.Stderr && term.IsTerminal(int(os.Stderr.Fd())) {
			writers = append(writers, os.Stderr)
		}
	} else {
		// No file sink: stderr is the only output (matches the historic default).
		writers = append(writers, os.Stderr)
	}

	var w io.Writer
	if len(writers) == 1 {
		w = writers[0]
	} else {
		w = io.MultiWriter(writers...)
	}

	handler := slog.NewTextHandler(w, &slog.HandlerOptions{
		Level:       opts.Level,
		ReplaceAttr: replaceLevel,
	})
	return slog.New(handler), closer, nil
}

// replaceLevel renders LevelVerbose as "VERBOSE" (slog would otherwise print
// "DEBUG+2"). Other levels keep their default names.
func replaceLevel(_ []string, a slog.Attr) slog.Attr {
	if a.Key != slog.LevelKey {
		return a
	}
	if lvl, ok := a.Value.Any().(slog.Level); ok && lvl == LevelVerbose {
		a.Value = slog.StringValue("VERBOSE")
	}
	return a
}

// Verbose logs at LevelVerbose (a convenience mirroring slog.Logger.Info/Debug,
// which have no verbose sibling).
func Verbose(log *slog.Logger, msg string, args ...any) {
	if log == nil {
		return
	}
	log.Log(context.Background(), LevelVerbose, msg, args...)
}

type noopCloser struct{}

func (noopCloser) Close() error { return nil }
