package enroll

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// mockCentral is an httptest stand-in for the central agent plane. It enforces
// the same single-use token / single-use enroll_secret semantics central does
// (backend/filearr/agentsync) so the client contract is exercised faithfully.
type mockCentral struct {
	srv *httptest.Server
	ca  *testCA // OTTs are minted by the in-process CA

	mu sync.Mutex
	// registered maps a valid unused token -> true; consumed on first register.
	tokens map[string]bool
	// per-agent enroll secret (consumed on first successful bind).
	agents map[string]*mockAgent
	// nilOTT forces the ca_ott-null fail-safe path.
	nilOTT bool

	// captured for assertions.
	LastBindFingerprint string
	RegisterCount       int
	BindCount           int
}

type mockAgent struct {
	id           string
	rolloutGroup string
	enrollSecret string
	secretUsed   bool
	fingerprint  string
}

func newMockCentral(t *testing.T, ca *testCA) *mockCentral {
	t.Helper()
	mc := &mockCentral{
		ca:     ca,
		tokens: map[string]bool{},
		agents: map[string]*mockAgent{},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/register", mc.handleRegister)
	// certificate bind: /api/v1/agents/{id}/certificate
	mux.HandleFunc("/api/v1/agents/", mc.handleAgentSubpath)
	mc.srv = httptest.NewServer(mux)
	t.Cleanup(mc.srv.Close)
	return mc
}

func (mc *mockCentral) URL() string { return mc.srv.URL }

// addToken registers a valid single-use enrollment token.
func (mc *mockCentral) addToken(tok string) {
	mc.mu.Lock()
	defer mc.mu.Unlock()
	mc.tokens[tok] = true
}

func (mc *mockCentral) handleRegister(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method", http.StatusMethodNotAllowed)
		return
	}
	if ct := r.Header.Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		writeDetail(w, http.StatusUnsupportedMediaType, "expected application/json")
		return
	}
	var req RegisterRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeDetail(w, http.StatusBadRequest, "bad json")
		return
	}

	mc.mu.Lock()
	defer mc.mu.Unlock()
	mc.RegisterCount++

	if !mc.tokens[req.Token] {
		// Unknown or already-consumed token -> 401, matching central.
		writeDetail(w, http.StatusUnauthorized, "enrollment token unknown or consumed")
		return
	}
	delete(mc.tokens, req.Token) // single-use

	agentID := newTestAgentIDFor(req.Token)
	secret := "secret-" + randToken()
	ag := &mockAgent{id: agentID, rolloutGroup: "default", enrollSecret: secret}
	mc.agents[agentID] = ag

	var ott *string
	if !mc.nilOTT {
		s := mc.ca.mintOTTForID(agentID)
		ott = &s
	}
	resp := RegisterResponse{
		AgentID:      agentID,
		RolloutGroup: ag.rolloutGroup,
		Status:       "pending",
		EnrollSecret: secret,
		CA: CABootstrap{
			URL:          mc.ca.URL,
			Fingerprint:  mc.ca.RootSHA256,
			Provisioner:  mc.ca.provName,
			CertTTLHours: 48,
		},
		CaOTT: ott,
	}
	writeJSON(w, http.StatusCreated, resp)
}

func (mc *mockCentral) handleAgentSubpath(w http.ResponseWriter, r *http.Request) {
	// Expect /api/v1/agents/{id}/certificate
	rest := strings.TrimPrefix(r.URL.Path, "/api/v1/agents/")
	parts := strings.Split(rest, "/")
	if len(parts) != 2 || parts[1] != "certificate" {
		http.NotFound(w, r)
		return
	}
	agentID := parts[0]
	if r.Method != http.MethodPost {
		http.Error(w, "method", http.StatusMethodNotAllowed)
		return
	}
	var req BindRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeDetail(w, http.StatusBadRequest, "bad json")
		return
	}

	mc.mu.Lock()
	defer mc.mu.Unlock()
	mc.BindCount++

	ag := mc.agents[agentID]
	if ag == nil {
		writeDetail(w, http.StatusNotFound, "no such agent")
		return
	}
	// Idempotent re-bind of the same fingerprint.
	if ag.fingerprint != "" {
		if ag.fingerprint == req.CertFingerprint {
			writeJSON(w, http.StatusOK, agentOut(ag))
			return
		}
		writeDetail(w, http.StatusConflict, "agent already has a certificate")
		return
	}
	if ag.secretUsed || req.EnrollSecret != ag.enrollSecret {
		writeDetail(w, http.StatusUnauthorized, "invalid enrollment secret")
		return
	}
	ag.secretUsed = true
	ag.fingerprint = req.CertFingerprint
	mc.LastBindFingerprint = req.CertFingerprint
	writeJSON(w, http.StatusOK, agentOut(ag))
}

func agentOut(ag *mockAgent) AgentResponse {
	return AgentResponse{ID: ag.id, Status: "active", CertFingerprint: ag.fingerprint}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeDetail(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, map[string]string{"detail": detail})
}
