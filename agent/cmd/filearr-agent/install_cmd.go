package main

import (
	"flag"
	"fmt"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/filearr/filearr/agent/internal/agentlog"
	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/install"
)

// runInstall installs the agent as a system service: resolve the OS layout,
// place the binary, optionally enroll (when a token is configured and the agent
// is not already enrolled), then register + start an auto-start,
// restart-on-failure service. Idempotent — a re-run upgrades in place. Requires
// administrator/root.
func runInstall(args []string) error {
	fs := newFlagSet("install")
	cfg := bindCommonFlags(fs)
	fs.StringVar(&cfg.Token, "token", envOr(envToken, activeSidecar().EnrollmentToken), "single-use enrollment token (else taken from the sidecar / env)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	set := flagsSet(fs)

	layout, err := install.ResolveLayout(runtime.GOOS, os.Getenv)
	if err != nil {
		return err
	}
	eff := effectiveLayout(cfg, set, layout)

	exe, err := os.Executable()
	if err != nil {
		return fmt.Errorf("resolve current executable: %w", err)
	}

	sc := activeSidecar()
	svcCfg := install.ServiceConfig(eff, sc.Path, runtime.GOOS)
	ctrl, err := install.NewController(install.NoopProgram{}, svcCfg)
	if err != nil {
		return err
	}

	dataDir := eff.DataDir
	enrollFn := func() error {
		central := cfg.CentralURL
		if central == "" {
			return fmt.Errorf("central URL required to enroll during install (set central_url in the sidecar, -central, or %s)", envCentralURL)
		}
		hostname, _ := os.Hostname()
		if hostname == "" {
			hostname = "filearr-agent"
		}
		name := cfg.Name
		if name == "" {
			name = hostname
		}
		enroller := &enroll.Enroller{
			Central:      enroll.NewCentralClient(central),
			Store:        enroll.NewCertStore(dataDir),
			Token:        cfg.Token,
			Hostname:     hostname,
			Platform:     enroll.DetectPlatform(),
			Name:         name,
			AgentVersion: Version,
		}
		ctx, cancel := signalContext()
		defer cancel()
		if _, err := enroller.Enroll(ctx); err != nil {
			return err
		}
		// One-shot token contract also applies to install-time enroll: erase the
		// spent token from the sidecar it came from.
		if sc.EnrollmentToken != "" && sc.Path != "" {
			if cerr := sc.ConsumeToken(time.Now()); cerr != nil {
				newLogger().Warn("could not rewrite sidecar to consume the enrollment token", "path", sc.Path, "err", cerr)
			}
		}
		return nil
	}

	inst := &install.Installer{
		Layout:    eff,
		SourceExe: exe,
		FS:        install.OSFS{},
		Service:   ctrl,
		IsAdmin:   install.IsAdmin,
		Enrolled:  func() bool { _, e := enroll.NewCertStore(dataDir).LoadState(); return e == nil },
		Enroll:    enrollFn,
		HasToken:  cfg.Token != "",
		Log:       newLogger(),
	}
	if err := inst.Install(); err != nil {
		return err
	}
	fmt.Printf("filearr-agent installed as service %q and started\n", install.ServiceName)
	fmt.Printf("  binary : %s\n", eff.BinPath)
	fmt.Printf("  data   : %s\n", eff.DataDir)
	fmt.Printf("  logs   : %s\n", eff.LogDir)
	fmt.Printf("  config : %s\n", eff.ConfigPath)
	return nil
}

// runUninstall stops + deregisters the service and removes the installed binary.
// --purge additionally deletes the data/logs/config directories (default keeps
// them and prints what was kept). Requires administrator/root.
func runUninstall(args []string) error {
	fs := newFlagSet("uninstall")
	cfg := bindCommonFlags(fs)
	purge := fs.Bool("purge", false, "also delete the data, logs, and config directories (default: keep them)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	set := flagsSet(fs)

	layout, err := install.ResolveLayout(runtime.GOOS, os.Getenv)
	if err != nil {
		return err
	}
	eff := effectiveLayout(cfg, set, layout)

	svcCfg := install.ServiceConfig(eff, activeSidecar().Path, runtime.GOOS)
	ctrl, err := install.NewController(install.NoopProgram{}, svcCfg)
	if err != nil {
		return err
	}
	inst := &install.Installer{
		Layout:  eff,
		FS:      install.OSFS{},
		Service: ctrl,
		IsAdmin: install.IsAdmin,
		Log:     newLogger(),
	}
	kept, err := inst.Uninstall(*purge)
	if err != nil {
		return err
	}
	fmt.Printf("filearr-agent service %q uninstalled\n", install.ServiceName)
	if len(kept) > 0 {
		fmt.Printf("kept (use --purge to remove): %s\n", strings.Join(kept, ", "))
	}
	return nil
}

// runService is the thin lifecycle wrapper: service status|start|stop|restart.
func runService(args []string) error {
	fs := newFlagSet("service")
	_ = bindCommonFlags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}
	action := fs.Arg(0)
	if action == "" {
		return fmt.Errorf("usage: filearr-agent service status|start|stop|restart")
	}

	layout, err := install.ResolveLayout(runtime.GOOS, os.Getenv)
	if err != nil {
		return err
	}
	svcCfg := install.ServiceConfig(layout, activeSidecar().Path, runtime.GOOS)
	ctrl, err := install.NewController(install.NoopProgram{}, svcCfg)
	if err != nil {
		return err
	}

	switch action {
	case "status":
		st, serr := ctrl.Status()
		if serr != nil {
			return serr
		}
		fmt.Printf("filearr-agent service: %s\n", st)
		return nil
	case "start":
		if err := ctrl.Start(); err != nil {
			return err
		}
		fmt.Println("filearr-agent service started")
		agentlog.Verbose(newLogger(), "service start requested")
		return nil
	case "stop":
		if err := ctrl.Stop(); err != nil {
			return err
		}
		fmt.Println("filearr-agent service stopped")
		return nil
	case "restart":
		if err := ctrl.Restart(); err != nil {
			return err
		}
		fmt.Println("filearr-agent service restarted")
		return nil
	default:
		return fmt.Errorf("unknown service action %q (want status|start|stop|restart)", action)
	}
}

// effectiveLayout adjusts the resolved OS layout with the operator's chosen data
// and log directories. A service install defaults data to the SYSTEM layout dir
// (not the per-user default that bindCommonFlags would otherwise pick when
// nothing is configured), because the service runs machine-wide. An explicit
// -data flag, FILEARR_AGENT_DATA_DIR, or a sidecar data_dir overrides it; a
// configured log dir (flag/env/sidecar) overrides the layout log dir.
func effectiveLayout(cfg *config, set map[string]bool, layout install.Layout) install.Layout {
	eff := layout
	if set["data"] || os.Getenv(envDataDir) != "" || activeSidecar().DataDir != "" {
		eff.DataDir = cfg.DataDir
	}
	if cfg.LogDir != "" {
		eff.LogDir = cfg.LogDir
	}
	return eff
}

// flagsSet returns the set of flag names explicitly provided on the command line
// (via flag.FlagSet.Visit), so the resolver can distinguish "operator chose the
// default value" from "value was defaulted".
func flagsSet(fs *flag.FlagSet) map[string]bool {
	set := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { set[f.Name] = true })
	return set
}
