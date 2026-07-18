package enroll

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"runtime"

	"github.com/smallstep/certificates/api"
	"github.com/smallstep/certificates/ca"
)

// CAClient is the subset of *ca.Client the enroll path needs. Declaring it as
// an interface keeps Enroller unit-testable and documents the exact surface
// (Sign to obtain the leaf, Roots to pin trust anchors).
type CAClient interface {
	Sign(req *api.SignRequest) (*api.SignResponse, error)
	Roots() (*api.RootsResponse, error)
}

// caClientFactory builds a CA client pinned to the given root SHA-256. The
// default uses the smallstep ca package; tests override it to inject an
// in-process authority's client.
type caClientFactory func(caURL, rootSHA256 string) (CAClient, error)

func defaultCAClientFactory(caURL, rootSHA256 string) (CAClient, error) {
	// WithRootSHA256 bootstraps trust by fetching /root/{sha} and pinning it —
	// no root file needs to pre-exist on disk. This is the same pin central
	// hands the agent in ca.fingerprint.
	client, err := ca.NewClient(caURL, ca.WithRootSHA256(rootSHA256))
	if err != nil {
		return nil, fmt.Errorf("build CA client: %w", err)
	}
	return client, nil
}

// Enroller runs the one-shot enrollment: register with central, exchange the
// scoped OTT with step-ca for a client cert, persist it, and bind its
// fingerprint back to central (docs/ops/agents.md §3).
type Enroller struct {
	Central *CentralClient
	Store   *CertStore

	Token        string
	Hostname     string
	Platform     string // windows | macos | linux; empty => detect from GOOS
	Name         string
	AgentVersion string

	// caFactory is overridable for tests; nil => defaultCAClientFactory.
	caFactory caClientFactory
}

// Result summarises a successful enrollment.
type Result struct {
	AgentID         string
	RolloutGroup    string
	CertFingerprint string
	Leaf            *x509.Certificate
}

// ErrCAOTTUnavailable is returned when central registered the agent but could
// not mint a step-ca OTT (its provisioner JWK is unset/malformed). The register
// step CONSUMED the single-use enrollment token and this client keeps no pending
// state, so the honest recovery is: fix the JWK server-side, mint a NEW token,
// re-run enroll (and revoke the orphaned pending agent row). The §7.3 ca-ott
// re-issue endpoint exists for operators, but this client has no resume flow to
// consume a re-issued OTT — do not promise one here.
var ErrCAOTTUnavailable = errors.New(
	"central registered the agent but returned a null ca_ott — its CA provisioner JWK " +
		"(FILEARR_CA_PROVISIONER_JWK) is unset or malformed (see docs/ops/agents.md §7.2; " +
		"Proxmox deploys: rerun deploy-proxmox.sh and watch the [agents] lines). " +
		"The enrollment token was consumed by registration: after fixing the JWK, mint a " +
		"NEW token and re-run enroll; the orphaned pending agent can be revoked in Admin → Agents")

// Enroll executes the full handshake and returns once the cert is persisted and
// bound. It is safe to retry: register consumes a single-use token, so a retry
// needs a fresh token (a replay yields a 401 from central).
func (e *Enroller) Enroll(ctx context.Context) (*Result, error) {
	platform := e.Platform
	if platform == "" {
		platform = DetectPlatform()
	}

	// (1) Register — the token is the credential; central assigns agent_id.
	reg, err := e.Central.Register(ctx, RegisterRequest{
		Token:        e.Token,
		Hostname:     e.Hostname,
		Platform:     platform,
		Name:         e.Name,
		AgentVersion: e.AgentVersion,
	})
	if err != nil {
		return nil, err
	}
	if reg.CaOTT == nil || *reg.CaOTT == "" {
		return nil, ErrCAOTTUnavailable
	}

	// (2)+(3) Build the CSR from the OTT and get it signed by step-ca.
	//
	// We use ca.CreateSignRequest(ott) rather than hand-building the CSR: it
	// parses the OTT's own claims and sets the CSR CommonName = sub and the
	// SANs = the OTT sans (central mints sub == sans == agent_id, R3), then
	// generates a fresh P-256 key. This is exactly the CN/SAN identity step-ca
	// validates the CSR against, so the library flow guarantees CSR==OTT by
	// construction — a hand-built CSR would only risk drifting from that
	// contract. A bare UUID string is neither IP/email/URI, so x509util.SplitSANs
	// classifies it as a DNS SAN (verified against step-ca's JWK validation in
	// the protocol tests).
	factory := e.caFactory
	if factory == nil {
		factory = defaultCAClientFactory
	}
	caClient, err := factory(reg.CA.URL, reg.CA.Fingerprint)
	if err != nil {
		return nil, err
	}

	signReq, key, err := ca.CreateSignRequest(*reg.CaOTT)
	if err != nil {
		return nil, fmt.Errorf("create sign request from ott: %w", err)
	}
	signResp, err := caClient.Sign(signReq)
	if err != nil {
		return nil, fmt.Errorf("step-ca sign: %w", err)
	}
	leaf, chain, err := certsFromSignResponse(signResp)
	if err != nil {
		return nil, err
	}

	// Fetch the CA roots to persist as trust anchors for later renewal.
	rootsResp, err := caClient.Roots()
	if err != nil {
		return nil, fmt.Errorf("fetch CA roots: %w", err)
	}
	roots := make([]*x509.Certificate, 0, len(rootsResp.Certificates))
	for _, c := range rootsResp.Certificates {
		roots = append(roots, c.Certificate)
	}

	// (4) Persist key + cert + roots + state atomically.
	if err := e.Store.SaveIdentity(Identity{
		Key:   key,
		Leaf:  leaf,
		Chain: chain,
		Roots: roots,
		State: State{
			AgentID:      reg.AgentID,
			CentralURL:   e.Central.BaseURL,
			RolloutGroup: reg.RolloutGroup,
			CAURL:        reg.CA.URL,
			CARootSHA256: reg.CA.Fingerprint,
		},
	}); err != nil {
		return nil, fmt.Errorf("persist identity: %w", err)
	}

	// (5) Bind the fingerprint back to central (pending -> active).
	fp := CertFingerprint(leaf)
	if _, err := e.Central.BindCertificate(ctx, reg.AgentID, BindRequest{
		EnrollSecret:    reg.EnrollSecret,
		CertFingerprint: fp,
	}); err != nil {
		return nil, err
	}

	return &Result{
		AgentID:         reg.AgentID,
		RolloutGroup:    reg.RolloutGroup,
		CertFingerprint: fp,
		Leaf:            leaf,
	}, nil
}

// certsFromSignResponse extracts the leaf and its issuing chain from a step-ca
// sign/renew response. ServerPEM is the leaf; CertChainPEM is [leaf, ...chain]
// so we drop its first element to avoid duplicating the leaf.
func certsFromSignResponse(resp *api.SignResponse) (*x509.Certificate, []*x509.Certificate, error) {
	if resp.ServerPEM.Certificate == nil {
		return nil, nil, fmt.Errorf("sign response missing leaf certificate")
	}
	leaf := resp.ServerPEM.Certificate
	var chain []*x509.Certificate
	for i, c := range resp.CertChainPEM {
		if i == 0 && c.Certificate != nil && c.Certificate.Equal(leaf) {
			continue // chain[0] repeats the leaf
		}
		if c.Certificate != nil {
			chain = append(chain, c.Certificate)
		}
	}
	// Fall back to the issuing CA cert if the chain came back empty.
	if len(chain) == 0 && resp.CaPEM.Certificate != nil {
		chain = append(chain, resp.CaPEM.Certificate)
	}
	return leaf, chain, nil
}

// DetectPlatform maps the Go runtime OS onto central's platform vocabulary
// (windows | macos | linux). Unlisted OSes fall back to linux, which central
// accepts; the field is descriptive, not security-relevant.
func DetectPlatform() string {
	switch runtime.GOOS {
	case "windows":
		return "windows"
	case "darwin":
		return "macos"
	default:
		return "linux"
	}
}
