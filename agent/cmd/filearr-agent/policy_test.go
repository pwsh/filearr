package main

import (
	"strings"
	"testing"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
)

// writePolicy persists a policy body into dir's policy.json via the cache.
func writePolicy(t *testing.T, dir string, version int, body string) {
	t.Helper()
	if err := agentcfg.NewETagCache(dir).Save(agentcfg.PolicyDoc{
		Scope: "library", Version: version, AppliedVersion: version,
		Policy: []byte(body),
	}); err != nil {
		t.Fatal(err)
	}
}

func TestApplyPolicyToScanOverlayPrecedence(t *testing.T) {
	dir := t.TempDir()
	writePolicy(t, dir, 3, `{"presets":["system_files"],"content_hash_max_bytes":2048}`)

	sc := scanConfig{
		Roots:          []string{"/data"},
		Presets:        []string{"local_only"},
		IncludeGlobs:   []string{"*.keep"},
		ContentCeiling: 999,
	}
	watch := false
	got, disabled := applyPolicyToScan(dir, sc, &watch)

	if strings.Join(got.Presets, ",") != "system_files" {
		t.Errorf("policy presets must override scan.json, got %v", got.Presets)
	}
	if got.ContentCeiling != 2048 {
		t.Errorf("policy content ceiling must win, got %d", got.ContentCeiling)
	}
	// Absent policy key keeps local include globs.
	if strings.Join(got.IncludeGlobs, ",") != "*.keep" {
		t.Errorf("absent include_globs must keep local, got %v", got.IncludeGlobs)
	}
	if disabled {
		t.Error("watch was not requested; must not report disabled")
	}
}

func TestApplyPolicyToScanWatchGating(t *testing.T) {
	dir := t.TempDir()
	writePolicy(t, dir, 1, `{"watch_mode":false}`)

	watch := true
	_, disabled := applyPolicyToScan(dir, scanConfig{Roots: []string{"/d"}}, &watch)
	if !disabled || watch {
		t.Errorf("watch_mode=false must gate --watch off: disabled=%v watch=%v", disabled, watch)
	}
}

func TestApplyPolicyToScanWatchAllowed(t *testing.T) {
	dir := t.TempDir()
	writePolicy(t, dir, 1, `{"watch_mode":true}`)

	watch := true
	_, disabled := applyPolicyToScan(dir, scanConfig{Roots: []string{"/d"}}, &watch)
	if disabled || !watch {
		t.Errorf("watch_mode=true must leave --watch on: disabled=%v watch=%v", disabled, watch)
	}
}

func TestApplyPolicyToScanNoCacheIsNoop(t *testing.T) {
	dir := t.TempDir() // no policy.json
	sc := scanConfig{Roots: []string{"/d"}, Presets: []string{"local_only"}}
	watch := true
	got, disabled := applyPolicyToScan(dir, sc, &watch)
	if strings.Join(got.Presets, ",") != "local_only" || disabled || !watch {
		t.Errorf("no cached policy must be a no-op: presets=%v disabled=%v watch=%v", got.Presets, disabled, watch)
	}
}
