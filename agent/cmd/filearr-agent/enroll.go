package main

import (
	"fmt"
	"os"

	"github.com/filearr/filearr/agent/internal/enroll"
)

// runEnroll performs the one-shot register -> CSR/sign -> persist -> bind
// handshake. Flags/behavior are byte-compatible with the pre-P7-T3 dispatch (it
// runs under urfave's SkipFlagParsing and keeps its own stdlib flag.FlagSet).
func runEnroll(args []string) error {
	fs := newFlagSet("enroll")
	cfg := bindCommonFlags(fs)
	fs.StringVar(&cfg.Token, "token", envOr(envToken, ""), "single-use enrollment token")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if err := cfg.requireCentralURL(); err != nil {
		return err
	}
	if cfg.Token == "" {
		return fmt.Errorf("enrollment token is required (-token or %s)", envToken)
	}

	hostname, _ := os.Hostname()
	if hostname == "" {
		hostname = "filearr-agent"
	}
	// Default the friendly name to the device hostname so `-name` is optional —
	// most fleets want exactly that, and a blank name renders badly in the panel.
	if cfg.Name == "" {
		cfg.Name = hostname
	}

	ctx, cancel := signalContext()
	defer cancel()

	enroller := &enroll.Enroller{
		Central:      enroll.NewCentralClient(cfg.CentralURL),
		Store:        enroll.NewCertStore(cfg.DataDir),
		Token:        cfg.Token,
		Hostname:     hostname,
		Platform:     enroll.DetectPlatform(),
		Name:         cfg.Name,
		AgentVersion: Version,
	}
	res, err := enroller.Enroll(ctx)
	if err != nil {
		return err
	}
	fmt.Printf("enrolled: agent_id=%s rollout_group=%s status=active\n", res.AgentID, res.RolloutGroup)
	fmt.Printf("cert_fingerprint=%s\n", res.CertFingerprint)
	fmt.Printf("data_dir=%s\n", cfg.DataDir)
	return nil
}
