package update

import (
	"context"
	"crypto/ed25519"
	"fmt"
	"io"
	"log/slog"
	"math/rand"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

// Defaults for the poll loop and health window.
const (
	defaultInterval     = 6 * time.Hour   // long interval — updates are not urgent
	defaultTimeout      = 5 * time.Minute // per HTTP call (a download can be large)
	defaultHealthWindow = 60 * time.Second
	maxBackoff          = 30 * time.Minute
)

// downloadSubdir is where artifacts are staged under DataDir before the swap.
const downloadSubdir = "updates"

// Config wires an Updater. Zero-valued optional fields take the defaults above.
type Config struct {
	BaseURL string
	AgentID string

	// AuthFn returns the per-request bearer token (agent cert fingerprint),
	// exactly as the replicator/policy/command clients use.
	AuthFn func() string

	HTTP *http.Client

	// DataDir holds the download staging dir + the boot-counter state file.
	DataDir string
	// CurrentVersion is this running binary's version (main.Version). Compared
	// against the manifest to decide "is this newer", and reported to central.
	CurrentVersion string
	// PublicKey is the pinned release-signing key. When nil the updater refuses
	// every update (fail-closed). Injectable for tests; production passes PinnedKey().
	PublicKey ed25519.PublicKey

	// ExePath overrides the binary path to swap (defaults to os.Executable()).
	ExePath string
	// Platform / Arch override the artifact match keys (default: this runtime).
	Platform string
	Arch     string

	Interval     time.Duration
	HealthWindow time.Duration

	Logger *slog.Logger
	Clock  func() time.Time
	Rand   *rand.Rand
	// reExec is the swap re-exec seam (overridable in tests so a swap does not
	// actually fork the test binary). nil => update.ReExec.
	reExec func(path string, argv []string) error
	// exit is called after a successful swap/rollback re-exec. nil => os.Exit.
	exit func(int)
}

// Updater performs the fetch/verify/download/swap + boot-count rollback cycle.
type Updater struct {
	cfg    Config
	client *client
	log    *slog.Logger
	clock  func() time.Time
	rnd    *rand.Rand
}

// New builds an Updater, applying defaults.
func New(cfg Config) *Updater {
	if cfg.HTTP == nil {
		cfg.HTTP = &http.Client{Timeout: defaultTimeout}
	}
	if cfg.Interval <= 0 {
		cfg.Interval = defaultInterval
	}
	if cfg.HealthWindow <= 0 {
		cfg.HealthWindow = defaultHealthWindow
	}
	if cfg.Platform == "" {
		cfg.Platform = CurrentPlatform()
	}
	if cfg.Arch == "" {
		cfg.Arch = runtime.GOARCH
	}
	if cfg.Logger == nil {
		cfg.Logger = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if cfg.Clock == nil {
		cfg.Clock = time.Now
	}
	if cfg.Rand == nil {
		cfg.Rand = rand.New(rand.NewSource(time.Now().UnixNano()))
	}
	if cfg.AuthFn == nil {
		cfg.AuthFn = func() string { return "" }
	}
	if cfg.reExec == nil {
		cfg.reExec = ReExec
	}
	if cfg.exit == nil {
		cfg.exit = os.Exit
	}
	return &Updater{
		cfg: cfg,
		client: &client{
			baseURL: strings.TrimRight(cfg.BaseURL, "/"),
			agentID: cfg.AgentID,
			authFn:  cfg.AuthFn,
			http:    cfg.HTTP,
		},
		log:   cfg.Logger,
		clock: cfg.Clock,
		rnd:   cfg.Rand,
	}
}

// CurrentPlatform maps the Go runtime OS onto the manifest's platform vocabulary
// (windows | macos | linux). Matches enroll.DetectPlatform.
func CurrentPlatform() string {
	switch runtime.GOOS {
	case "windows":
		return "windows"
	case "darwin":
		return "macos"
	default:
		return "linux"
	}
}

// CheckForUpdate fetches + verifies the manifest and returns the newer artifact
// when one is offered. ok=false means "nothing to do" (up to date, no covering
// release, or no artifact for this platform/arch). A verification failure is a
// hard error (the update is refused, never silently skipped as "up to date").
func (u *Updater) CheckForUpdate(ctx context.Context) (*Manifest, *Artifact, bool, error) {
	m, err := u.client.fetchManifest(ctx, u.cfg.CurrentVersion)
	if err != nil {
		return nil, nil, false, err
	}
	if m == nil {
		return nil, nil, false, nil // central says up to date (204)
	}
	if err := Verify(*m, u.cfg.PublicKey); err != nil {
		return nil, nil, false, fmt.Errorf("refusing update %s: %w", m.Version, err)
	}
	if !IsNewer(m.Version, u.cfg.CurrentVersion) {
		return m, nil, false, nil // signed, but not actually newer
	}
	a, found := m.FindArtifact(u.cfg.Platform, u.cfg.Arch)
	if !found {
		return m, nil, false, fmt.Errorf("update %s has no artifact for %s/%s", m.Version, u.cfg.Platform, u.cfg.Arch)
	}
	return m, &a, true, nil
}

// ApplyUpdate downloads + sha256-verifies the artifact, writes the boot-counter
// state, performs the A/B swap, and re-execs into the new binary (then exits).
// It returns only on a pre-swap failure; on success the process is replaced.
func (u *Updater) ApplyUpdate(ctx context.Context, m *Manifest, a *Artifact) error {
	exe := u.cfg.ExePath
	if exe == "" {
		p, err := os.Executable()
		if err != nil {
			return fmt.Errorf("resolve current executable: %w", err)
		}
		exe = p
	}
	dir := filepath.Join(u.cfg.DataDir, downloadSubdir)
	newBinary, err := u.client.downloadArtifact(ctx, m.Version, *a, dir)
	if err != nil {
		return err
	}
	u.log.Info("update downloaded + verified; swapping", "version", m.Version, "artifact", a.URL)

	// Write the boot-counter BEFORE the swap so a crash between swap and re-exec
	// still leaves a recoverable trial record (research §5.3 step 1).
	previous := PreviousBinaryPath(exe)
	if err := SaveState(u.cfg.DataDir, State{
		NewVersion:         m.Version,
		PreviousBinaryPath: previous,
		Attempts:           0,
		MaxAttempts:        DefaultMaxAttempts,
	}); err != nil {
		_ = os.Remove(newBinary)
		return err
	}
	if _, err := Apply(newBinary, exe); err != nil {
		_ = ClearState(u.cfg.DataDir) // swap did not happen — no trial pending
		_ = os.Remove(newBinary)
		return err
	}
	u.log.Info("update applied; re-executing", "version", m.Version, "path", exe)
	if err := u.cfg.reExec(exe, os.Args); err != nil {
		// The new binary is in place but re-exec failed; the service supervisor
		// will restart us into it. Leave the state file for the boot check.
		u.log.Error("re-exec after swap failed; relying on supervisor restart", "err", err)
		return err
	}
	u.cfg.exit(0)
	return nil // unreachable in production (exit); reachable when exit is stubbed in tests
}

// BootCheck runs the rollback state machine at daemon start (research §5.3). It
// MUST be called before the long-running loops. Outcomes:
//
//   - no pending update -> returns (healthPending=false); boot normally.
//   - a trial is pending and within budget -> increments the boot counter,
//     returns healthPending=true; the caller runs the health window then calls
//     ConfirmHealthy on success.
//   - the trial has exhausted its budget -> restores the previous binary and
//     re-execs it (then exits); does not return.
func (u *Updater) BootCheck(ctx context.Context) (healthPending bool, err error) {
	st, err := LoadState(u.cfg.DataDir)
	if err != nil {
		// A corrupt state file must not wedge boot: log + clear + proceed.
		u.log.Error("unreadable update state; clearing and continuing", "err", err)
		_ = ClearState(u.cfg.DataDir)
		return false, nil
	}
	decision, next := Evaluate(st)
	switch decision {
	case DecisionNone:
		return false, nil
	case DecisionRollback:
		exe := u.cfg.ExePath
		if exe == "" {
			p, e := os.Executable()
			if e != nil {
				return false, fmt.Errorf("rollback: resolve executable: %w", e)
			}
			exe = p
		}
		u.log.Warn("update failed health checks; rolling back",
			"new_version", st.NewVersion, "attempts", st.Attempts, "previous", st.PreviousBinaryPath)
		if _, statErr := os.Stat(st.PreviousBinaryPath); statErr != nil {
			// Nothing to roll back to; clear and continue on the current binary.
			u.log.Error("rollback target missing; clearing state and continuing", "err", statErr)
			_ = ClearState(u.cfg.DataDir)
			return false, nil
		}
		if err := Restore(st.PreviousBinaryPath, exe); err != nil {
			return false, fmt.Errorf("rollback restore: %w", err)
		}
		_ = ClearState(u.cfg.DataDir)
		if err := u.cfg.reExec(exe, os.Args); err != nil {
			return false, fmt.Errorf("rollback re-exec: %w", err)
		}
		u.cfg.exit(0)
		return false, nil // unreachable in production
	default: // DecisionHealthCheck
		if err := SaveState(u.cfg.DataDir, *next); err != nil {
			return false, err
		}
		u.log.Info("update on trial this boot", "new_version", next.NewVersion,
			"attempt", next.Attempts, "max", next.MaxAttempts)
		return true, nil
	}
}

// ConfirmHealthy clears the boot-counter, deletes the rollback (.old) binary,
// and reports the running version to central (the §6.3 confirmed-version
// signal). Called after the health window passes.
func (u *Updater) ConfirmHealthy(ctx context.Context) {
	st, _ := LoadState(u.cfg.DataDir)
	if st != nil && st.PreviousBinaryPath != "" {
		_ = os.Remove(st.PreviousBinaryPath)
	}
	if err := ClearState(u.cfg.DataDir); err != nil {
		u.log.Warn("clear update state after healthy boot failed", "err", err)
	}
	// A best-effort manifest poll reports our now-confirmed running version. An
	// offline agent simply skips it (clean offline behavior is still "healthy").
	if _, err := u.client.fetchManifest(ctx, u.cfg.CurrentVersion); err != nil {
		u.log.Debug("post-health version report skipped (offline?)", "err", err)
	}
	u.log.Info("update confirmed healthy", "version", u.cfg.CurrentVersion)
}

// RunHealthWindow waits HealthWindow (or until ctx is cancelled) and, on a clean
// pass (no crash/cancel), calls ConfirmHealthy. Run in a goroutine after
// BootCheck returns healthPending=true.
func (u *Updater) RunHealthWindow(ctx context.Context) {
	t := time.NewTimer(u.cfg.HealthWindow)
	defer t.Stop()
	select {
	case <-ctx.Done():
		// Shutdown before the window closed: leave the state file so the next
		// boot re-trials (it did not prove itself healthy).
		return
	case <-t.C:
		u.ConfirmHealthy(context.WithoutCancel(ctx))
	}
}

// Run is the long-interval poll loop for the daemon: check for an update every
// Interval (±10% jitter), applying one when offered. A fetch/apply failure backs
// off (capped) and never exits; only ctx cancellation returns.
func (u *Updater) Run(ctx context.Context) error {
	backoff := time.Duration(0)
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		err := u.checkAndApply(ctx)
		var wait time.Duration
		if err != nil {
			if backoff == 0 {
				backoff = u.cfg.Interval
			} else {
				backoff *= 2
			}
			if backoff > maxBackoff {
				backoff = maxBackoff
			}
			u.log.Warn("update check failed; backing off", "backoff", backoff.String(), "err", err)
			wait = backoff
		} else {
			backoff = 0
			wait = u.jittered(u.cfg.Interval)
		}
		if !sleepCtx(ctx, wait) {
			return ctx.Err()
		}
	}
}

func (u *Updater) checkAndApply(ctx context.Context) error {
	m, a, ok, err := u.CheckForUpdate(ctx)
	if err != nil {
		return err
	}
	if !ok {
		return nil
	}
	u.log.Info("update available", "version", m.Version, "current", u.cfg.CurrentVersion)
	return u.ApplyUpdate(ctx, m, a)
}

func (u *Updater) jittered(d time.Duration) time.Duration {
	if d <= 0 {
		return d
	}
	delta := float64(d) * 0.1
	return d + time.Duration((u.rnd.Float64()*2-1)*delta)
}

func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		select {
		case <-ctx.Done():
			return false
		default:
			return true
		}
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
