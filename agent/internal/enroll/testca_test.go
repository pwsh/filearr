package enroll

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	stepca "github.com/smallstep/certificates/ca"

	"go.step.sm/crypto/jose"
	"go.step.sm/crypto/minica"
)

// TestMain silences step-ca's chatty std-logger output so `go test` stays
// readable; the CA logs startup + per-request lines via the stdlib logger.
func TestMain(m *testing.M) {
	log.SetOutput(io.Discard)
	os.Exit(m.Run())
}

// caParams tunes the in-process authority's JWK-provisioner claims. The default
// mirrors docs/ops/agents.md §7.1 (min 24h / default 48h / max 72h,
// allowRenewalAfterExpiry). The soak variant uses seconds-scale durations with
// backdate disabled so renewal cycles complete inside a test.
type caParams struct {
	minTLS, defTLS, maxTLS  string
	backdate                string // "" => step-ca default (1m); "0s" for short TTLs
	allowRenewalAfterExpiry bool
}

func defaultCAParams() caParams {
	return caParams{minTLS: "24h", defTLS: "48h", maxTLS: "72h", allowRenewalAfterExpiry: true}
}

func shortTTLCAParams() caParams {
	// backdate must be 0 or a short cert (NotAfter = NotBefore + defTLS) would be
	// born already-expired under step-ca's default 1-minute backdate.
	return caParams{minTLS: "2s", defTLS: "6s", maxTLS: "60s", backdate: "0s", allowRenewalAfterExpiry: true}
}

// testCA is a real smallstep/certificates authority running in-process on a
// loopback TLS listener, plus the provisioner private JWK so tests can mint
// OTTs with the exact ES256 claim shape as central's agentsync.mint_ca_ott.
type testCA struct {
	URL        string
	RootSHA256 string
	Root       *x509.Certificate
	provName   string
	provJWK    *jose.JSONWebKey // private key, for minting OTTs
}

func newTestCA(t *testing.T, p caParams) *testCA {
	t.Helper()

	m, err := minica.New(minica.WithName("Filearr Test"))
	if err != nil {
		t.Fatalf("minica.New: %v", err)
	}
	dir := t.TempDir()
	rootPath := filepath.Join(dir, "root.crt")
	intPath := filepath.Join(dir, "intermediate.crt")
	keyPath := filepath.Join(dir, "intermediate.key")
	writePEM(t, rootPath, "CERTIFICATE", m.Root.Raw)
	writePEM(t, intPath, "CERTIFICATE", m.Intermediate.Raw)
	intKeyDER, err := x509.MarshalPKCS8PrivateKey(m.Signer)
	if err != nil {
		t.Fatalf("marshal intermediate key: %v", err)
	}
	writePEM(t, keyPath, "PRIVATE KEY", intKeyDER)

	// Provisioner keypair: public half goes in ca.json, private half mints OTTs.
	const provName = "filearr-agents"
	jwk, err := jose.GenerateJWK("EC", "P-256", "ES256", "sig", "filearr-agents-kid", 0)
	if err != nil {
		t.Fatalf("generate provisioner jwk: %v", err)
	}
	pubJWK := jwk.Public()
	pubJSON, err := json.Marshal(pubJWK)
	if err != nil {
		t.Fatalf("marshal public jwk: %v", err)
	}

	addr := reserveLoopbackAddr(t)
	claims := map[string]any{
		"minTLSCertDuration":      p.minTLS,
		"maxTLSCertDuration":      p.maxTLS,
		"defaultTLSCertDuration":  p.defTLS,
		"allowRenewalAfterExpiry": p.allowRenewalAfterExpiry,
	}
	authority := map[string]any{
		"provisioners": []any{
			map[string]any{
				"type":   "JWK",
				"name":   provName,
				"key":    json.RawMessage(pubJSON),
				"claims": claims,
			},
		},
	}
	if p.backdate != "" {
		authority["backdate"] = p.backdate
	}
	caCfg := map[string]any{
		"root":      rootPath,
		"crt":       intPath,
		"key":       keyPath,
		"address":   addr,
		"dnsNames":  []string{"127.0.0.1", "localhost"},
		"authority": authority,
		"logger":    map[string]any{"format": "text"},
	}
	cfgPath := filepath.Join(dir, "ca.json")
	cfgBytes, err := json.MarshalIndent(caCfg, "", "  ")
	if err != nil {
		t.Fatalf("marshal ca.json: %v", err)
	}
	if err := os.WriteFile(cfgPath, cfgBytes, 0o600); err != nil {
		t.Fatalf("write ca.json: %v", err)
	}

	cfg, err := loadCAConfig(cfgPath)
	if err != nil {
		t.Fatalf("load ca config: %v", err)
	}
	authorityCA, err := stepca.New(cfg, stepca.WithConfigFile(cfgPath))
	if err != nil {
		t.Fatalf("build authority: %v", err)
	}
	go func() { _ = authorityCA.Run() }()
	t.Cleanup(func() { _ = authorityCA.Stop() })

	url := "https://" + addr
	waitForCA(t, addr)

	sum := sha256.Sum256(m.Root.Raw)
	return &testCA{
		URL:        url,
		RootSHA256: hex.EncodeToString(sum[:]),
		Root:       m.Root,
		provName:   provName,
		provJWK:    jwk,
	}
}

// ottOpts overrides fields of a minted OTT for negative-path tests. Zero fields
// take safe defaults (sub=agentID, sans=[agentID], iat/nbf=now, exp=now+5m,
// random jti).
type ottOpts struct {
	sub  string
	sans []string
	iat  time.Time
	nbf  time.Time
	exp  time.Time
	jti  string
}

// mintOTT signs a JWK one-time token for agentID with the same claim shape as
// central's agentsync.mint_ca_ott: iss=provisioner, aud=<caURL>/1.0/sign,
// sub/sans=agentID, iat/nbf/exp, unique jti; header alg ES256, typ JWT, kid.
func (c *testCA) mintOTT(t *testing.T, agentID string, opts ...ottOpts) string {
	t.Helper()
	var o ottOpts
	if len(opts) > 0 {
		o = opts[0]
	}
	tok, err := c.mintOTTErr(agentID, o)
	if err != nil {
		t.Fatalf("mint ott: %v", err)
	}
	return tok
}

// mintOTTForID is the no-*testing.T variant used inside the mock-central HTTP
// handler. It panics on the impossible signing error (test context only).
func (c *testCA) mintOTTForID(agentID string) string {
	tok, err := c.mintOTTErr(agentID, ottOpts{})
	if err != nil {
		panic(err)
	}
	return tok
}

func (c *testCA) mintOTTErr(agentID string, o ottOpts) (string, error) {
	sub := o.sub
	if sub == "" {
		sub = agentID
	}
	sans := o.sans
	if sans == nil {
		sans = []string{sub}
	}
	now := time.Now()
	iat := o.iat
	if iat.IsZero() {
		iat = now
	}
	nbf := o.nbf
	if nbf.IsZero() {
		nbf = iat
	}
	exp := o.exp
	if exp.IsZero() {
		exp = iat.Add(5 * time.Minute)
	}
	jti := o.jti
	if jti == "" {
		b := make([]byte, 16)
		_, _ = rand.Read(b)
		jti = hex.EncodeToString(b)
	}

	so := new(jose.SignerOptions)
	so.WithType("JWT")
	so.WithHeader("kid", c.provJWK.KeyID)
	sig, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.ES256, Key: c.provJWK.Key}, so)
	if err != nil {
		return "", err
	}

	claims := struct {
		jose.Claims
		SANs []string `json:"sans"`
	}{
		Claims: jose.Claims{
			ID:        jti,
			Subject:   sub,
			Issuer:    c.provName,
			Audience:  jose.Audience{c.URL + "/1.0/sign"},
			IssuedAt:  jose.NewNumericDate(iat),
			NotBefore: jose.NewNumericDate(nbf),
			Expiry:    jose.NewNumericDate(exp),
		},
		SANs: sans,
	}
	return jose.Signed(sig).Claims(claims).CompactSerialize()
}

// --- helpers ---------------------------------------------------------------

func writePEM(t *testing.T, path, typ string, der []byte) {
	t.Helper()
	buf := pem.EncodeToMemory(&pem.Block{Type: typ, Bytes: der})
	if err := os.WriteFile(path, buf, 0o600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

// reserveLoopbackAddr binds an ephemeral loopback port, records it, and frees
// it so step-ca can rebind. A small TOCTOU window is acceptable for tests.
func reserveLoopbackAddr(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve port: %v", err)
	}
	addr := l.Addr().String()
	_ = l.Close()
	return addr
}

// waitForCA polls the CA /health endpoint until it responds 200 or times out.
func waitForCA(t *testing.T, addr string) {
	t.Helper()
	client := &http.Client{
		Timeout: 500 * time.Millisecond,
		Transport: &http.Transport{
			TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec // readiness probe only
		},
	}
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := client.Get("https://" + addr + "/health")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("CA at %s did not become ready", addr)
}
