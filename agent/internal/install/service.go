package install

import (
	"errors"

	"github.com/kardianos/service"
)

// Service identity constants (shared by install, uninstall, and the lifecycle
// wrappers so they all target the same registered unit).
const (
	ServiceName        = "filearr-agent"
	ServiceDisplayName = "Filearr Agent"
	ServiceDescription = "Filearr distributed agent — local scanning, replication to central, and offline local query."
)

// ServiceEnvMarker is set in the service's environment by install so the running
// daemon can detect it is service-managed and take the clean-exit-for-restart
// path on self-update instead of self-re-exec (which a service manager would
// otherwise race). The value is "1".
const ServiceEnvMarker = "FILEARR_AGENT_SERVICE"

// ServiceConfig builds the kardianos service definition for an install. The
// service runs `<bin> run --data <dataDir> --log-dir <logDir> [--config <path>]`
// with auto-start + restart-on-failure per OS (see restartOptions). goos selects
// the restart Option keys.
func ServiceConfig(layout Layout, sidecarPath, goos string) *service.Config {
	args := []string{"run", "--data", layout.DataDir, "--log-dir", layout.LogDir}
	if sidecarPath != "" {
		args = append(args, "--config", sidecarPath)
	}
	return &service.Config{
		Name:        ServiceName,
		DisplayName: ServiceDisplayName,
		Description: ServiceDescription,
		Executable:  layout.BinPath,
		Arguments:   args,
		EnvVars:     map[string]string{ServiceEnvMarker: "1"},
		Option:      restartOptions(goos),
	}
}

// RunServiceConfig is the minimal config used by the `run` command when it wraps
// the daemon under kardianos (interactive or service-managed). It needs only the
// identity fields; the executable/arguments/restart policy are an install-time
// concern already baked into the registered unit.
func RunServiceConfig() *service.Config {
	return &service.Config{
		Name:        ServiceName,
		DisplayName: ServiceDisplayName,
		Description: ServiceDescription,
	}
}

// restartOptions returns the per-OS auto-restart Option keys kardianos honors.
//
//   - linux (systemd): Restart=on-failure. NOTE: kardianos v1.2.4 hardcodes
//     RestartSec=120 in its unit template (not Option-configurable), so the
//     requested 5s becomes 120s here — documented, not silently dropped.
//   - darwin (launchd): KeepAlive + RunAtLoad, so launchd relaunches the daemon
//     on any exit (including the update restart-exit) and at boot.
//   - windows (SCM): OnFailure=restart with a 5s delay and a 10s reset window,
//     mapped by kardianos to the service's recovery actions.
func restartOptions(goos string) service.KeyValue {
	switch goos {
	case "linux":
		return service.KeyValue{"Restart": "on-failure"}
	case "darwin":
		return service.KeyValue{"KeepAlive": true, "RunAtLoad": true}
	case "windows":
		return service.KeyValue{
			"OnFailure":              "restart",
			"OnFailureDelayDuration": "5s",
			"OnFailureResetPeriod":   10,
		}
	default:
		return service.KeyValue{}
	}
}

// kardianosController adapts a kardianos service.Service to the Controller
// interface, normalising Status across OSes.
type kardianosController struct {
	svc service.Service
}

// NewController wraps a kardianos service for lifecycle management. prog is the
// service.Interface; for pure management (install/uninstall/status/start/stop)
// a no-op program suffices because those calls never invoke Start/Stop on it.
func NewController(prog service.Interface, cfg *service.Config) (Controller, error) {
	svc, err := service.New(prog, cfg)
	if err != nil {
		return nil, err
	}
	return &kardianosController{svc: svc}, nil
}

func (c *kardianosController) Install() error   { return c.svc.Install() }
func (c *kardianosController) Uninstall() error { return c.svc.Uninstall() }
func (c *kardianosController) Start() error     { return c.svc.Start() }
func (c *kardianosController) Stop() error      { return c.svc.Stop() }
func (c *kardianosController) Restart() error   { return c.svc.Restart() }

func (c *kardianosController) Status() (Status, error) {
	st, err := c.svc.Status()
	if err != nil {
		if errors.Is(err, service.ErrNotInstalled) {
			return StatusNotInstalled, nil
		}
		return StatusUnknown, err
	}
	switch st {
	case service.StatusRunning:
		return StatusRunning, nil
	case service.StatusStopped:
		return StatusStopped, nil
	default:
		return StatusUnknown, nil
	}
}

// NoopProgram is a service.Interface that does nothing, used to build a
// Controller purely for management calls.
type NoopProgram struct{}

func (NoopProgram) Start(service.Service) error { return nil }
func (NoopProgram) Stop(service.Service) error  { return nil }

// String renders a Status for CLI output.
func (s Status) String() string {
	switch s {
	case StatusRunning:
		return "running"
	case StatusStopped:
		return "stopped"
	case StatusNotInstalled:
		return "not installed"
	default:
		return "unknown"
	}
}
