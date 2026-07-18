package commands

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

// stagingStatus mirrors central's staging status JSON (api/agent_staging.py
// _status_dict). total_bytes is a pointer because central may not know it yet.
type stagingStatus struct {
	ID               string `json:"id"`
	State            string `json:"state"`
	BytesTransferred int64  `json:"bytes_transferred"`
	TotalBytes       *int64 `json:"total_bytes"`
}

func (s stagingStatus) ref() StagingRef {
	r := StagingRef{ID: s.ID, State: s.State, BytesTransferred: s.BytesTransferred}
	if s.TotalBytes != nil {
		r.TotalBytes = *s.TotalBytes
	}
	return r
}

// Attach implements Uploader: POST /agents/{id}/staging {command_id, total_bytes}
// -> the created (201) or re-attached (200) transfer status. Idempotent per
// command_id server-side, so a restarted agent re-attaches the same row.
func (p *Poller) Attach(ctx context.Context, commandID string, totalBytes int64) (StagingRef, error) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/staging", p.baseURL, p.agentID)
	body := map[string]any{"command_id": commandID, "total_bytes": totalBytes}
	status, resp, err := p.post(ctx, url, body)
	if err != nil {
		return StagingRef{}, err
	}
	if status != http.StatusOK && status != http.StatusCreated {
		return StagingRef{}, p.statusError("staging attach", status, resp)
	}
	var s stagingStatus
	if err := json.Unmarshal(resp, &s); err != nil {
		return StagingRef{}, fmt.Errorf("staging attach: decode body: %w", err)
	}
	return s.ref(), nil
}

// Append implements Uploader: PATCH /agents/{id}/staging/{transfer_id} with the
// Upload-Offset header and the raw chunk body. A 409 offset mismatch is returned
// as *OffsetMismatchError carrying central's committed offset (from the response's
// Upload-Offset header, or the JSON {"offset"} fallback).
func (p *Poller) Append(ctx context.Context, transferID string, offset int64, chunk []byte) (StagingRef, error) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/staging/%s", p.baseURL, p.agentID, transferID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPatch, url, bytes.NewReader(chunk))
	if err != nil {
		return StagingRef{}, err
	}
	req.Header.Set("Content-Type", "application/offset+octet-stream")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Upload-Offset", fmt.Sprintf("%d", offset))
	if tok := p.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	resp, err := p.http.Do(req)
	if err != nil {
		return StagingRef{}, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))

	if resp.StatusCode == http.StatusConflict {
		// Prefer the header (always present); fall back to the JSON offset field.
		if h := resp.Header.Get("Upload-Offset"); h != "" {
			var srv int64
			if _, perr := fmt.Sscanf(h, "%d", &srv); perr == nil {
				return StagingRef{}, &OffsetMismatchError{Offset: srv}
			}
		}
		var env struct {
			Reason string `json:"reason"`
			Offset int64  `json:"offset"`
		}
		if json.Unmarshal(respBody, &env) == nil && env.Reason == "offset_mismatch" {
			return StagingRef{}, &OffsetMismatchError{Offset: env.Offset}
		}
		return StagingRef{}, p.statusError("staging append", resp.StatusCode, respBody)
	}
	if resp.StatusCode != http.StatusOK {
		return StagingRef{}, p.statusError("staging append", resp.StatusCode, respBody)
	}
	var s stagingStatus
	if err := json.Unmarshal(respBody, &s); err != nil {
		return StagingRef{}, fmt.Errorf("staging append: decode body: %w", err)
	}
	return s.ref(), nil
}

// processStageUpload runs a stage_upload: read the per-agent rate cap from the
// cached policy (at upload START — a mid-upload policy change applies next time),
// stream the file to central's staging area (resumable, rate-limited, path
// re-validated), heartbeating the command lease throughout, then complete the
// command with the transfer id + total bytes acked.
func (p *Poller) processStageUpload(ctx context.Context, cmd commandOut) {
	hbCtx, cancel := context.WithCancel(ctx)
	go p.heartbeat(hbCtx, cmd.ID)
	defer cancel()

	var rateCap int64
	if p.rateProvider != nil {
		rateCap = p.rateProvider()
	}
	res, err := p.exec.StageUpload(ctx, cmd.ID, cmd.Payload, p, rateCap)
	cancel() // stop the heartbeat before reporting the terminal result
	if err != nil {
		p.log.Warn("stage_upload failed", "command_id", cmd.ID, "err", err)
		p.complete(ctx, cmd.ID, false, map[string]any{"error": err.Error()})
		return
	}
	p.complete(ctx, cmd.ID, true, map[string]any{
		"transfer_id": res.TransferID,
		"total_bytes": res.TotalBytes,
	})
}
