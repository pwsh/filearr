package outbox

import (
	"testing"
	"time"
)

func TestBackoffExponentialCapAndReset(t *testing.T) {
	b := NewBackoff(BackoffConfig{Min: time.Second, Max: 8 * time.Second, Factor: 2})
	want := []time.Duration{1, 2, 4, 8, 8, 8}
	for i, w := range want {
		if got := b.Next(); got != w*time.Second {
			t.Errorf("Next()#%d = %v, want %v", i, got, w*time.Second)
		}
	}
	b.Reset()
	if got := b.Next(); got != time.Second {
		t.Errorf("after Reset, Next() = %v, want floor 1s", got)
	}
}

func TestBackoffDefaults(t *testing.T) {
	b := NewBackoff(BackoffConfig{})
	if b.Min != defaultBackoffMin || b.Max != defaultBackoffMax || b.Factor != defaultBackoffFactor {
		t.Errorf("defaults not applied: %+v", b)
	}
	// Cap must be ~5 minutes per the design.
	if b.Max != 5*time.Minute {
		t.Errorf("backoff cap = %v, want 5m", b.Max)
	}
}
