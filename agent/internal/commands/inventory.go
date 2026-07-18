package commands

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"

	"github.com/filearr/filearr/agent/internal/inventory"
)

// KindInventory is the W6-D3 extensible-inventory command kind.
const KindInventory = "inventory"

// decodeInventoryPayload narrows the JSONB command payload into an
// inventory.Command. It is tolerant of absent/typed-loosely fields (JSON numbers
// decode to float64) so a hand-authored or forward-compat payload does not fail
// the whole command; unknown keys are ignored (the vocabulary the agent honors is
// what it advertised).
func decodeInventoryPayload(raw map[string]any) inventory.Command {
	return inventory.Command{
		Collectors:   stringSlice(raw["collectors"]),
		Preset:       stringOf(raw["preset"]),
		Paths:        stringSlice(raw["paths"]),
		IncludeRegex: stringSlice(raw["include_regex"]),
		ExcludeRegex: stringSlice(raw["exclude_regex"]),
		MaxEntries:   intOf(raw["max_entries"]),
		MaxDepth:     intOf(raw["max_depth"]),
	}
}

func stringOf(v any) string {
	s, _ := v.(string)
	return s
}

func stringSlice(v any) []string {
	arr, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, e := range arr {
		if s, ok := e.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func intOf(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case int64:
		return int(n)
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	default:
		return 0
	}
}

// processInventory runs an inventory command and reports its result, heartbeating
// the lease throughout (a broad walk can outlast one lease). A small result inlines
// its {summary, entries} in the command completion; a large result is gzipped and
// uploaded to the inventory-results endpoint, with the completion carrying only
// {summary, result_ref}. An agent build without an inventory Runner completes
// ok=false (graceful degradation).
func (p *Poller) processInventory(ctx context.Context, cmd commandOut) {
	if p.inv == nil {
		p.complete(ctx, cmd.ID, false, map[string]any{"error": "inventory not supported by this agent"})
		return
	}
	hbCtx, cancel := context.WithCancel(ctx)
	go p.heartbeat(hbCtx, cmd.ID)
	defer cancel()

	res, err := p.inv.Run(ctx, decodeInventoryPayload(cmd.Payload))
	if err != nil {
		cancel()
		p.log.Warn("inventory command failed", "command_id", cmd.ID, "err", err)
		p.complete(ctx, cmd.ID, false, map[string]any{"error": err.Error()})
		return
	}

	summary := summaryMap(res.Summary)
	if res.Inlineable() {
		cancel()
		p.complete(ctx, cmd.ID, true, map[string]any{"summary": summary, "entries": res.Inline})
		return
	}

	// Large result: upload the gzip NDJSON blob, then complete with a ref.
	ref, uerr := p.uploadInventoryResult(ctx, cmd.ID, res.Blob)
	cancel()
	if uerr != nil {
		p.log.Warn("inventory result upload failed", "command_id", cmd.ID, "err", uerr)
		p.complete(ctx, cmd.ID, false, map[string]any{"summary": summary, "error": uerr.Error()})
		return
	}
	p.complete(ctx, cmd.ID, true, map[string]any{"summary": summary, "result_ref": ref})
}

// uploadInventoryResult POSTs the gzip NDJSON blob to central's inventory-results
// endpoint (a dedicated small-blob channel mirroring agent_thumbs' write-if-absent
// posture — NOT the staging plane, which is sized for multi-GB media and re-hashes
// against a catalog row). Returns the stored ref central echoes back.
func (p *Poller) uploadInventoryResult(ctx context.Context, commandID string, blob []byte) (string, error) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/inventory-results", p.baseURL, p.agentID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(blob))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/gzip")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-Filearr-Command-Id", commandID)
	if tok := p.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	resp, err := p.http.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return "", p.statusError("inventory-results", resp.StatusCode, body)
	}
	var env struct {
		ResultRef string `json:"result_ref"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return "", fmt.Errorf("inventory-results: decode body: %w", err)
	}
	return env.ResultRef, nil
}

// summaryMap converts the inventory Summary to the JSON map central stores in the
// completion result. json round-trips the struct's tags (the canonical shape).
func summaryMap(s inventory.Summary) map[string]any {
	b, _ := json.Marshal(s)
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	return m
}
