package config

import (
	"encoding/json"
	"strings"
	"testing"
)

func i64(v int64) *int64 { return &v }
func boolp(v bool) *bool { return &v }

func TestOverlayScanPolicyWins(t *testing.T) {
	local := ScanSettings{
		Presets:             []string{"local_preset"},
		IncludeGlobs:        []string{"*.local"},
		ExcludeGlobs:        []string{"skip/"},
		ContentCeilingBytes: 100,
	}
	// Policy sets presets + ceiling; leaves globs absent (nil).
	pol := Policy{
		Presets:             []string{"system_files", "caches_temp"},
		ContentHashMaxBytes: i64(4096),
	}
	got := pol.OverlayScan(local)

	if strings.Join(got.Presets, ",") != "system_files,caches_temp" {
		t.Errorf("policy presets must override scan.json, got %v", got.Presets)
	}
	if got.ContentCeilingBytes != 4096 {
		t.Errorf("policy content ceiling must win, got %d", got.ContentCeilingBytes)
	}
	// Absent policy keys keep the local values.
	if strings.Join(got.IncludeGlobs, ",") != "*.local" {
		t.Errorf("absent include_globs must keep local, got %v", got.IncludeGlobs)
	}
	if strings.Join(got.ExcludeGlobs, ",") != "skip/" {
		t.Errorf("absent exclude_globs must keep local, got %v", got.ExcludeGlobs)
	}
}

func TestOverlayScanEmptyPresetsIsExplicit(t *testing.T) {
	// A present-but-empty presets array explicitly clears the local presets
	// (distinct from absent). Absent (nil) keeps local.
	local := ScanSettings{Presets: []string{"local"}}

	explicitEmpty := Policy{Presets: []string{}}.OverlayScan(local)
	if len(explicitEmpty.Presets) != 0 {
		t.Errorf("explicit empty presets must clear local, got %v", explicitEmpty.Presets)
	}

	absent := Policy{}.OverlayScan(local)
	if strings.Join(absent.Presets, ",") != "local" {
		t.Errorf("absent presets must keep local, got %v", absent.Presets)
	}
}

func TestParsePolicyDistinguishesAbsentEmptyPresent(t *testing.T) {
	absent, err := ParsePolicy([]byte(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	if absent.Presets != nil || absent.WatchMode != nil || absent.PollIntervalSeconds != nil {
		t.Error("absent keys must decode to nil")
	}

	present, err := ParsePolicy([]byte(`{"presets":[],"watch_mode":false,"poll_interval_seconds":120}`))
	if err != nil {
		t.Fatal(err)
	}
	if present.Presets == nil || len(present.Presets) != 0 {
		t.Errorf("present empty presets must be non-nil empty, got %v", present.Presets)
	}
	if present.WatchMode == nil || *present.WatchMode {
		t.Error("watch_mode:false must decode to a non-nil false pointer")
	}
	if present.PollIntervalSeconds == nil || *present.PollIntervalSeconds != 120 {
		t.Error("poll_interval_seconds must decode")
	}
}

func TestUnknownKeysRoundTripByteLevel(t *testing.T) {
	// The contract: unknown keys must survive fetch → persist → reload →
	// re-serialize. Store the raw body verbatim; the typed view never drops keys.
	raw := json.RawMessage(`{"presets":["a"],"future_knob":42,"nested":{"x":true}}`)
	doc := PolicyDoc{Scope: "library", Version: 3, Policy: raw}

	dir := t.TempDir()
	cache := NewETagCache(dir)
	if err := cache.Save(doc); err != nil {
		t.Fatal(err)
	}
	reloaded, ok, err := cache.Load()
	if err != nil || !ok {
		t.Fatalf("reload: ok=%v err=%v", ok, err)
	}

	// Re-serialize the reloaded policy body and assert the unknown key survives.
	reserialized, err := json.Marshal(reloaded.Policy)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"future_knob", "42", "nested"} {
		if !strings.Contains(string(reserialized), want) {
			t.Errorf("unknown content %q lost across round-trip: %s", want, reserialized)
		}
	}

	// The honored view still reads the known key.
	pol, err := reloaded.Parsed()
	if err != nil {
		t.Fatal(err)
	}
	if len(pol.Presets) != 1 || pol.Presets[0] != "a" {
		t.Errorf("known key mis-parsed: %v", pol.Presets)
	}

	// PolicyKeys exposes the unknown key for the CLI.
	keys := reloaded.PolicyKeys()
	if strings.Join(keys, ",") != "future_knob,nested,presets" {
		t.Errorf("PolicyKeys = %v", keys)
	}
}

func TestPollIntervalFloorAndDefault(t *testing.T) {
	def := DefaultPollInterval
	floor := MinPollInterval

	if got := (Policy{}).PollInterval(def, floor); got != def {
		t.Errorf("absent poll interval must use default, got %s", got)
	}
	// Below floor is clamped up.
	below := 5
	if got := (Policy{PollIntervalSeconds: &below}).PollInterval(def, floor); got != floor {
		t.Errorf("below-floor poll interval must clamp to floor, got %s", got)
	}
	ok := 90
	if got := (Policy{PollIntervalSeconds: &ok}).PollInterval(def, floor); got != 90*1e9 {
		t.Errorf("in-range poll interval must pass through, got %s", got)
	}
}

func TestWatchAndReconcileAccessors(t *testing.T) {
	if _, set := (Policy{}).WatchAllowed(); set {
		t.Error("absent watch_mode must report set=false")
	}
	allowed, set := (Policy{WatchMode: boolp(false)}).WatchAllowed()
	if !set || allowed {
		t.Errorf("watch_mode:false must be set=true allowed=false, got set=%v allowed=%v", set, allowed)
	}
	if _, ok := (Policy{}).ReconcileInterval(); ok {
		t.Error("absent reconcile interval must report ok=false")
	}
	secs := 3600
	d, ok := (Policy{ReconcileIntervalSeconds: &secs}).ReconcileInterval()
	if !ok || d != 3600*1e9 {
		t.Errorf("reconcile interval = %s ok=%v", d, ok)
	}
}
