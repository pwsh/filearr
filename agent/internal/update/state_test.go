package update

import "testing"

// TestEvaluateNilAndBudget covers the pure decision core.
func TestEvaluateNil(t *testing.T) {
	if d, _ := Evaluate(nil); d != DecisionNone {
		t.Fatalf("nil state: got %v, want DecisionNone", d)
	}
}

// TestCrashLoopRollsBackWithinBudget simulates a binary that never proves
// healthy: each boot increments the counter, and the 4th boot (after 3 crashed
// trial windows) rolls back — matching research §5.3 ("crashes 3 times in a row,
// the next launch rolls back").
func TestCrashLoopRollsBackWithinBudget(t *testing.T) {
	st := &State{NewVersion: "1.4.0", PreviousBinaryPath: "/tmp/old", Attempts: 0, MaxAttempts: 3}
	for boot := 1; boot <= 3; boot++ {
		d, next := Evaluate(st)
		if d != DecisionHealthCheck {
			t.Fatalf("boot %d: got %v, want DecisionHealthCheck", boot, d)
		}
		if next.Attempts != boot {
			t.Fatalf("boot %d: attempts=%d, want %d", boot, next.Attempts, boot)
		}
		st = next // persist (no MarkHealthy => the trial "crashed")
	}
	// 4th boot: budget exhausted -> rollback.
	if d, _ := Evaluate(st); d != DecisionRollback {
		t.Fatalf("boot 4: got %v, want DecisionRollback", d)
	}
}

// TestHealthyBootClears models a good update: after one trial the caller clears
// the state, so a subsequent boot sees no pending update.
func TestHealthyBootClearsState(t *testing.T) {
	dir := t.TempDir()
	if err := SaveState(dir, State{NewVersion: "1.4.0", PreviousBinaryPath: "/tmp/old", MaxAttempts: 3}); err != nil {
		t.Fatalf("save: %v", err)
	}
	st, err := LoadState(dir)
	if err != nil || st == nil {
		t.Fatalf("load: st=%v err=%v", st, err)
	}
	d, next := Evaluate(st)
	if d != DecisionHealthCheck || next.Attempts != 1 {
		t.Fatalf("first trial: d=%v attempts=%d", d, next.Attempts)
	}
	// Health passes -> clear.
	if err := ClearState(dir); err != nil {
		t.Fatalf("clear: %v", err)
	}
	st, err = LoadState(dir)
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	if st != nil {
		t.Fatalf("state not cleared: %+v", st)
	}
	if d, _ := Evaluate(st); d != DecisionNone {
		t.Fatalf("after clear: got %v, want DecisionNone", d)
	}
}

func TestZeroMaxAttemptsDefaults(t *testing.T) {
	// A state with MaxAttempts unset must default to DefaultMaxAttempts, not
	// roll back immediately (Attempts 0 >= 0 would be a footgun).
	st := &State{NewVersion: "1.4.0", MaxAttempts: 0}
	d, next := Evaluate(st)
	if d != DecisionHealthCheck {
		t.Fatalf("got %v, want DecisionHealthCheck", d)
	}
	if next.MaxAttempts != DefaultMaxAttempts {
		t.Fatalf("MaxAttempts=%d, want %d", next.MaxAttempts, DefaultMaxAttempts)
	}
}
