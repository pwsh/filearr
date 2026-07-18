package update

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// updateStateFile is the crash-loop boot-counter sidecar, written in DataDir
// right before an A/B swap and cleared once the new version proves healthy.
const updateStateFile = "update-state.json"

// DefaultMaxAttempts is the boot-count budget (research §5.3): after this many
// launches of the new binary WITHOUT a healthy window, the next boot rolls back.
const DefaultMaxAttempts = 3

// State is the boot-counting rollback record (research §5.3, modeled on
// systemd-boot "Automatic Boot Assessment"). Persisted as JSON in DataDir.
type State struct {
	NewVersion         string `json:"new_version"`
	PreviousBinaryPath string `json:"previous_binary_path"`
	Attempts           int    `json:"attempts"`
	MaxAttempts        int    `json:"max_attempts"`
}

// Decision is what a boot evaluation tells the daemon to do.
type Decision int

const (
	// DecisionNone — no pending update; boot normally.
	DecisionNone Decision = iota
	// DecisionHealthCheck — a pending update is on trial this boot; run the
	// health window and MarkHealthy on pass.
	DecisionHealthCheck
	// DecisionRollback — the update has exhausted its attempt budget; restore
	// PreviousBinaryPath over the current binary and re-exec it.
	DecisionRollback
)

// Evaluate is the PURE core of the state machine (unit-tested with no I/O). Given
// the loaded state (nil == no state file), it returns the boot decision and, for
// DecisionHealthCheck, the mutated state to persist (attempts incremented).
//
// Boot sequence (research §5.3 step 4 — "crashes 3 times in a row, the next
// launch rolls back"):
//
//	attempts starts at 0. Each trial boot that reaches this point without having
//	cleared the state increments attempts and runs a health window. Once attempts
//	has reached MaxAttempts (i.e. MaxAttempts crashed windows already happened),
//	the NEXT boot rolls back instead of trying again.
func Evaluate(st *State) (Decision, *State) {
	if st == nil {
		return DecisionNone, nil
	}
	maxA := st.MaxAttempts
	if maxA <= 0 {
		maxA = DefaultMaxAttempts
	}
	if st.Attempts >= maxA {
		return DecisionRollback, st
	}
	next := *st
	next.Attempts++
	next.MaxAttempts = maxA
	return DecisionHealthCheck, &next
}

// LoadState reads the boot-counter sidecar. It returns (nil, nil) when absent —
// the common no-pending-update case — and a wrapped error only for a present but
// unreadable/corrupt file.
func LoadState(dir string) (*State, error) {
	buf, err := os.ReadFile(filepath.Join(dir, updateStateFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("update: read state: %w", err)
	}
	var st State
	if err := json.Unmarshal(buf, &st); err != nil {
		return nil, fmt.Errorf("update: parse state: %w", err)
	}
	return &st, nil
}

// SaveState atomically writes the boot-counter sidecar.
func SaveState(dir string, st State) error {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("update: create data dir: %w", err)
	}
	buf, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return fmt.Errorf("update: marshal state: %w", err)
	}
	return atomicWrite(filepath.Join(dir, updateStateFile), append(buf, '\n'), 0o644)
}

// ClearState removes the boot-counter sidecar (update confirmed healthy, or
// rollback complete). A missing file is not an error.
func ClearState(dir string) error {
	err := os.Remove(filepath.Join(dir, updateStateFile))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("update: clear state: %w", err)
	}
	return nil
}
