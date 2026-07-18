package outbox

import "time"

// Backoff is a capped exponential backoff, reset on success. It backs the drain
// loop's block-don't-drop behaviour: while central is unreachable the drain waits
// out growing delays (never discarding unsent rows), then snaps back to the floor
// the instant a batch succeeds.
type Backoff struct {
	Min    time.Duration
	Max    time.Duration
	Factor float64

	cur time.Duration
}

// BackoffConfig seeds a Backoff; zero fields fall back to the defaults below.
type BackoffConfig struct {
	Min    time.Duration
	Max    time.Duration
	Factor float64
}

const (
	defaultBackoffMin    = 1 * time.Second
	defaultBackoffMax    = 5 * time.Minute
	defaultBackoffFactor = 2.0
)

// NewBackoff builds a Backoff from cfg, applying defaults for zero fields.
func NewBackoff(cfg BackoffConfig) *Backoff {
	b := &Backoff{Min: cfg.Min, Max: cfg.Max, Factor: cfg.Factor}
	if b.Min <= 0 {
		b.Min = defaultBackoffMin
	}
	if b.Max <= 0 {
		b.Max = defaultBackoffMax
	}
	if b.Factor <= 1 {
		b.Factor = defaultBackoffFactor
	}
	return b
}

// Next returns the current delay and advances the schedule (capped at Max). The
// first call after construction or Reset returns Min.
func (b *Backoff) Next() time.Duration {
	if b.cur <= 0 {
		b.cur = b.Min
		return b.cur
	}
	next := time.Duration(float64(b.cur) * b.Factor)
	if next > b.Max {
		next = b.Max
	}
	b.cur = next
	return b.cur
}

// Reset returns the schedule to its floor after a successful flush.
func (b *Backoff) Reset() { b.cur = 0 }
