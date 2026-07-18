package enroll

import (
	"crypto"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"os"
	"path/filepath"
)

const (
	keyFileName   = "agent.key"
	certFileName  = "agent.crt"  // leaf + issuing chain, PEM, leaf first
	rootsFileName = "roots.pem"  // CA root(s), PEM
	stateFileName = "state.json" // agent identity + endpoints
)

// State is the small non-secret sidecar recording who this agent is and where
// its trust anchors point. Persisted next to the key/cert so `run` can rebuild
// the renewal client without re-contacting central.
type State struct {
	AgentID      string `json:"agent_id"`
	CentralURL   string `json:"central_url"`
	RolloutGroup string `json:"rollout_group"`
	CAURL        string `json:"ca_url"`
	CARootSHA256 string `json:"ca_root_sha256"`
}

// Identity is the fully materialised on-disk enrollment: the private key, the
// leaf, its issuing chain, the trusted roots, and the sidecar state.
type Identity struct {
	Key   crypto.PrivateKey
	Leaf  *x509.Certificate
	Chain []*x509.Certificate // issuing intermediates (leaf excluded)
	Roots []*x509.Certificate
	State State
}

// CertStore persists and loads the enrollment material under a single data
// directory using atomic temp-then-rename writes.
//
// The private key is written 0600 on POSIX. On Windows the umask/mode bits do
// not translate to an ACL — the effective protection is the parent directory's
// inherited ACL (typically the user's profile / %AppData%). A hardened Windows
// deployment should additionally restrict the data dir ACL (out of scope here).
type CertStore struct {
	Dir string
}

// NewCertStore returns a store rooted at dir. The directory is created lazily
// on the first write.
func NewCertStore(dir string) *CertStore {
	return &CertStore{Dir: dir}
}

func (s *CertStore) path(name string) string { return filepath.Join(s.Dir, name) }

// SaveIdentity persists a freshly issued key + leaf + chain + roots + state.
// Used by the enroll path (new key). Writes are individually atomic; the key is
// written first so a partial failure never leaves a cert without its key.
func (s *CertStore) SaveIdentity(id Identity) error {
	if err := os.MkdirAll(s.Dir, 0o700); err != nil {
		return fmt.Errorf("create data dir: %w", err)
	}
	keyPEM, err := marshalKeyPEM(id.Key)
	if err != nil {
		return err
	}
	if err := atomicWrite(s.path(keyFileName), keyPEM, 0o600); err != nil {
		return fmt.Errorf("write key: %w", err)
	}
	if err := s.SaveCertificate(id.Leaf, id.Chain); err != nil {
		return err
	}
	if err := s.SaveRoots(id.Roots); err != nil {
		return err
	}
	if err := s.SaveState(id.State); err != nil {
		return err
	}
	return nil
}

// SaveCertificate atomically rewrites the leaf+chain PEM. Used by both enroll
// and renewal (renewal keeps the existing key, so only this file changes).
func (s *CertStore) SaveCertificate(leaf *x509.Certificate, chain []*x509.Certificate) error {
	if err := os.MkdirAll(s.Dir, 0o700); err != nil {
		return fmt.Errorf("create data dir: %w", err)
	}
	var buf []byte
	buf = append(buf, certToPEM(leaf)...)
	for _, c := range chain {
		buf = append(buf, certToPEM(c)...)
	}
	if err := atomicWrite(s.path(certFileName), buf, 0o644); err != nil {
		return fmt.Errorf("write cert: %w", err)
	}
	return nil
}

// SaveRoots atomically rewrites the trusted-roots PEM bundle.
func (s *CertStore) SaveRoots(roots []*x509.Certificate) error {
	if len(roots) == 0 {
		return nil
	}
	if err := os.MkdirAll(s.Dir, 0o700); err != nil {
		return fmt.Errorf("create data dir: %w", err)
	}
	var buf []byte
	for _, c := range roots {
		buf = append(buf, certToPEM(c)...)
	}
	if err := atomicWrite(s.path(rootsFileName), buf, 0o644); err != nil {
		return fmt.Errorf("write roots: %w", err)
	}
	return nil
}

// SaveState atomically rewrites the sidecar state file.
func (s *CertStore) SaveState(st State) error {
	if err := os.MkdirAll(s.Dir, 0o700); err != nil {
		return fmt.Errorf("create data dir: %w", err)
	}
	buf, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal state: %w", err)
	}
	buf = append(buf, '\n')
	if err := atomicWrite(s.path(stateFileName), buf, 0o644); err != nil {
		return fmt.Errorf("write state: %w", err)
	}
	return nil
}

// LoadState reads just the sidecar state (cheap; used before a full Load).
func (s *CertStore) LoadState() (State, error) {
	var st State
	buf, err := os.ReadFile(s.path(stateFileName))
	if err != nil {
		return st, fmt.Errorf("read state: %w", err)
	}
	if err := json.Unmarshal(buf, &st); err != nil {
		return st, fmt.Errorf("parse state: %w", err)
	}
	return st, nil
}

// Load reads and validates the full on-disk identity. It errors if any of the
// key/cert/state are missing or unparsable so `run` fails fast rather than
// starting a renewer with no cert.
func (s *CertStore) Load() (*Identity, error) {
	st, err := s.LoadState()
	if err != nil {
		return nil, err
	}
	key, err := s.loadKey()
	if err != nil {
		return nil, err
	}
	leaf, chain, err := s.loadCertChain()
	if err != nil {
		return nil, err
	}
	roots, err := s.loadRoots()
	if err != nil {
		return nil, err
	}
	return &Identity{Key: key, Leaf: leaf, Chain: chain, Roots: roots, State: st}, nil
}

// TLSCertificate builds the tls.Certificate (leaf + chain + private key) used
// as the client identity for mTLS renewal.
func (s *CertStore) TLSCertificate() (tls.Certificate, error) {
	certPEM, err := os.ReadFile(s.path(certFileName))
	if err != nil {
		return tls.Certificate{}, fmt.Errorf("read cert: %w", err)
	}
	keyPEM, err := os.ReadFile(s.path(keyFileName))
	if err != nil {
		return tls.Certificate{}, fmt.Errorf("read key: %w", err)
	}
	pair, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return tls.Certificate{}, fmt.Errorf("load key pair: %w", err)
	}
	return pair, nil
}

// RootPool builds an x509.CertPool from the persisted roots for verifying the
// CA server during renewal.
func (s *CertStore) RootPool() (*x509.CertPool, error) {
	roots, err := s.loadRoots()
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	for _, c := range roots {
		pool.AddCert(c)
	}
	return pool, nil
}

func (s *CertStore) loadKey() (crypto.PrivateKey, error) {
	buf, err := os.ReadFile(s.path(keyFileName))
	if err != nil {
		return nil, fmt.Errorf("read key: %w", err)
	}
	block, _ := pem.Decode(buf)
	if block == nil {
		return nil, fmt.Errorf("key file %s: no PEM block", keyFileName)
	}
	key, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse key: %w", err)
	}
	return key, nil
}

func (s *CertStore) loadCertChain() (*x509.Certificate, []*x509.Certificate, error) {
	buf, err := os.ReadFile(s.path(certFileName))
	if err != nil {
		return nil, nil, fmt.Errorf("read cert: %w", err)
	}
	certs, err := parseCertsPEM(buf)
	if err != nil {
		return nil, nil, fmt.Errorf("parse cert: %w", err)
	}
	if len(certs) == 0 {
		return nil, nil, fmt.Errorf("cert file %s: no certificates", certFileName)
	}
	return certs[0], certs[1:], nil
}

func (s *CertStore) loadRoots() ([]*x509.Certificate, error) {
	buf, err := os.ReadFile(s.path(rootsFileName))
	if err != nil {
		return nil, fmt.Errorf("read roots: %w", err)
	}
	certs, err := parseCertsPEM(buf)
	if err != nil {
		return nil, fmt.Errorf("parse roots: %w", err)
	}
	return certs, nil
}

// --- helpers ---------------------------------------------------------------

func marshalKeyPEM(key crypto.PrivateKey) ([]byte, error) {
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, fmt.Errorf("marshal key: %w", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), nil
}

func certToPEM(cert *x509.Certificate) []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw})
}

func parseCertsPEM(buf []byte) ([]*x509.Certificate, error) {
	var out []*x509.Certificate
	for {
		var block *pem.Block
		block, buf = pem.Decode(buf)
		if block == nil {
			break
		}
		if block.Type != "CERTIFICATE" {
			continue
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, err
		}
		out = append(out, cert)
	}
	return out, nil
}

// atomicWrite writes to a temp file in the same directory then renames over the
// target, so a reader never observes a half-written file. On Windows os.Rename
// replaces an existing target atomically for files on the same volume.
func atomicWrite(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmpName, perm); err != nil {
		return err
	}
	return os.Rename(tmpName, path)
}
