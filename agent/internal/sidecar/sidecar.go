// Package sidecar loads and rewrites the user-editable agent configuration file
// (filearr-agent.json). The sidecar is the lowest-precedence configuration
// source at runtime — explicit CLI flags override environment variables, which
// override the sidecar, which overrides the built-in defaults — so it exists to
// give an operator a single durable place to record enrollment + logging
// settings that survive service restarts without re-passing flags.
//
// Parsing is strict JSON (no comments) but tolerant of unknown keys, so a newer
// agent build can add fields without a rewrite breaking an older on-disk file,
// and a hand-written file with a typo'd key is not silently discarded wholesale.
// The raw key/value set is preserved across a rewrite (ConsumeToken), so
// forward-compatible unknown keys and the operator's own formatting choices are
// never dropped.
package sidecar

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"time"
)

// FileName is the fixed sidecar file name across every discovery location.
const FileName = "filearr-agent.json"

// Environment key naming the sidecar path (discovery step a's env equivalent).
const EnvConfigPath = "FILEARR_AGENT_CONFIG"

// JSON field names (kept as constants so the rewrite path and the schema stay in
// lockstep).
const (
	keyCentralURL      = "central_url"
	keyEnrollmentToken = "enrollment_token"
	keyTokenConsumedAt = "enrollment_token_consumed_at"
	keyAgentName       = "agent_name"
	keyConfigGroup     = "config_group"
	keyDataDir         = "data_dir"
	keyLogLevel        = "log_level"
	keyLogDir          = "log_dir"
)

// Config is the parsed sidecar. All fields are optional; a zero Config is the
// valid "nothing configured, use defaults" state. Path records where it was
// loaded from ("" when no file was found), and raw preserves the exact on-disk
// key set for a lossless rewrite.
type Config struct {
	CentralURL                string
	EnrollmentToken           string
	EnrollmentTokenConsumedAt string
	AgentName                 string
	ConfigGroup               string
	DataDir                   string
	LogLevel                  string
	LogDir                    string

	// Path is the file this config was loaded from, or "" if none was found.
	Path string
	// raw is the full decoded key set, preserved so ConsumeToken can rewrite the
	// file without dropping unknown (forward-compat) keys.
	raw map[string]json.RawMessage
}

// Resolver locates the sidecar file per the discovery order. It is a struct (not
// a bare function) so tests can inject GOOS + env + a stat function and exercise
// every branch of the discovery order deterministically on any host.
type Resolver struct {
	// Explicit is the --config flag / EnvConfigPath value (discovery step a).
	// When set it is used verbatim and a load failure is surfaced (the operator
	// asked for that exact file).
	Explicit string
	// ExePath is the running executable path (discovery step b looks beside it).
	ExePath string
	// GOOS selects the step-c OS config directory. Empty => runtime.GOOS.
	GOOS string
	// Getenv resolves environment variables (%ProgramData% on Windows). Empty =>
	// os.Getenv.
	Getenv func(string) string
	// stat reports whether a candidate path exists. Empty => os.Stat.
	stat func(string) (os.FileInfo, error)
}

// DefaultResolver builds a Resolver for the running process. explicit is the
// --config flag value (may be ""); the EnvConfigPath fallback is applied when
// explicit is empty.
func DefaultResolver(explicit string) *Resolver {
	if explicit == "" {
		explicit = os.Getenv(EnvConfigPath)
	}
	exe, _ := os.Executable()
	return &Resolver{Explicit: explicit, ExePath: exe, GOOS: runtime.GOOS}
}

func (r *Resolver) goos() string {
	if r.GOOS != "" {
		return r.GOOS
	}
	return runtime.GOOS
}

func (r *Resolver) getenv(k string) string {
	if r.Getenv != nil {
		return r.Getenv(k)
	}
	return os.Getenv(k)
}

func (r *Resolver) exists(path string) bool {
	stat := r.stat
	if stat == nil {
		stat = os.Stat
	}
	_, err := stat(path)
	return err == nil
}

// Discover returns the sidecar path and whether one was located. An explicit
// path is always returned (found=true) even if it does not exist, so Load can
// surface the operator's mistake; the beside-exe and OS-config-dir candidates
// are only returned when they actually exist.
func (r *Resolver) Discover() (path string, found bool) {
	// (a) explicit --config / env.
	if r.Explicit != "" {
		return r.Explicit, true
	}
	// (b) next to the executable.
	if r.ExePath != "" {
		cand := filepath.Join(filepath.Dir(r.ExePath), FileName)
		if r.exists(cand) {
			return cand, true
		}
	}
	// (c) OS config directory.
	if cand := OSConfigPath(r.goos(), r.getenv); cand != "" && r.exists(cand) {
		return cand, true
	}
	return "", false
}

// Load discovers and parses the sidecar. A missing sidecar (none of the
// discovery locations exist and no explicit path was given) is NOT an error: it
// returns an empty Config so callers uniformly fall through to env/defaults.
func (r *Resolver) Load() (*Config, error) {
	path, found := r.Discover()
	if !found {
		return &Config{}, nil
	}
	return LoadFile(path)
}

// LoadFile parses a specific sidecar file.
func LoadFile(path string) (*Config, error) {
	buf, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read sidecar %s: %w", path, err)
	}
	raw := map[string]json.RawMessage{}
	if err := json.Unmarshal(buf, &raw); err != nil {
		return nil, fmt.Errorf("parse sidecar %s: %w", path, err)
	}
	c := &Config{Path: path, raw: raw}
	// Unknown keys are tolerated: only the recognised keys are pulled into typed
	// fields; everything else lingers in raw and is preserved on rewrite.
	get := func(key string) string {
		v, ok := raw[key]
		if !ok {
			return ""
		}
		var s string
		if err := json.Unmarshal(v, &s); err != nil {
			return "" // wrong type for a string field: ignore, don't fail the whole load
		}
		return s
	}
	c.CentralURL = get(keyCentralURL)
	c.EnrollmentToken = get(keyEnrollmentToken)
	c.EnrollmentTokenConsumedAt = get(keyTokenConsumedAt)
	c.AgentName = get(keyAgentName)
	c.ConfigGroup = get(keyConfigGroup)
	c.DataDir = get(keyDataDir)
	c.LogLevel = get(keyLogLevel)
	c.LogDir = get(keyLogDir)
	return c, nil
}

// ConsumeToken implements the one-shot enrollment-token contract: after a
// successful enroll the spent token is erased from the sidecar and a
// machine-readable consumed-at marker is stamped, so the token is never left at
// rest and a later re-run does not attempt a replay (which central rejects). It
// is a no-op when the config came from no file or already carries no token. The
// rewrite preserves every other key (including unknown ones) byte-for-value and
// writes 0600 so the file at rest is owner-only.
func (c *Config) ConsumeToken(now time.Time) error {
	if c.Path == "" || c.EnrollmentToken == "" {
		return nil
	}
	c.EnrollmentToken = ""
	c.EnrollmentTokenConsumedAt = now.UTC().Format(time.RFC3339)
	return c.save(c.Path)
}

// save rewrites the sidecar from the preserved raw key set, applying the two
// token fields, then atomically replaces the file at 0600.
func (c *Config) save(path string) error {
	if c.raw == nil {
		c.raw = map[string]json.RawMessage{}
	}
	// Erase the spent token (store an empty string, never the secret) and stamp
	// the consumed-at marker.
	c.raw[keyEnrollmentToken] = mustJSON("")
	c.raw[keyTokenConsumedAt] = mustJSON(c.EnrollmentTokenConsumedAt)

	buf, err := marshalStable(c.raw)
	if err != nil {
		return err
	}
	return atomicWrite(path, buf, 0o600)
}

// marshalStable renders the raw key set with sorted keys + indentation so a
// rewrite is deterministic and diff-friendly.
func marshalStable(raw map[string]json.RawMessage) ([]byte, error) {
	keys := make([]string, 0, len(raw))
	for k := range raw {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var out bytes.Buffer
	out.WriteString("{\n")
	for i, k := range keys {
		kb, _ := json.Marshal(k)
		out.WriteString("  ")
		out.Write(kb)
		out.WriteString(": ")
		// Compact the value so it stays on one line; fall back to the raw bytes if
		// (impossibly) it is not valid JSON.
		var compact bytes.Buffer
		if err := json.Compact(&compact, raw[k]); err != nil {
			out.Write(raw[k])
		} else {
			out.Write(compact.Bytes())
		}
		if i < len(keys)-1 {
			out.WriteByte(',')
		}
		out.WriteByte('\n')
	}
	out.WriteString("}\n")
	return out.Bytes(), nil
}

func mustJSON(v any) json.RawMessage {
	b, _ := json.Marshal(v)
	return b
}

// OSConfigDir returns the step-c OS configuration directory for a GOOS. Paths
// are built with the target OS separator (not the host's) so the function is
// correct when cross-resolving in tests. getenv supplies %ProgramData% on
// Windows.
func OSConfigDir(goos string, getenv func(string) string) string {
	if getenv == nil {
		getenv = os.Getenv
	}
	switch goos {
	case "windows":
		base := getenv("ProgramData")
		if base == "" {
			base = `C:\ProgramData`
		}
		return base + `\Filearr Agent`
	case "darwin":
		return "/Library/Application Support/FilearrAgent"
	default: // linux + other unix
		return "/etc/filearr-agent"
	}
}

// atomicWrite writes to a temp file in the same directory then renames over the
// target so a reader never observes a half-written file. perm is applied to the
// temp file before the rename (0600 for the token-bearing sidecar). On POSIX the
// mode is the effective protection; on Windows the bits do not map to an ACL and
// the parent directory's inherited ACL governs (documented in install.md).
func atomicWrite(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmpName, perm); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}

// OSConfigPath is OSConfigDir joined with FileName using the target separator.
func OSConfigPath(goos string, getenv func(string) string) string {
	dir := OSConfigDir(goos, getenv)
	if dir == "" {
		return ""
	}
	sep := "/"
	if goos == "windows" {
		sep = `\`
	}
	return dir + sep + FileName
}
