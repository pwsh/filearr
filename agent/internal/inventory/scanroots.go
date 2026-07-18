package inventory

import (
	"encoding/json"

	"github.com/filearr/filearr/agent/internal/pathspec"
)

// scanSelection mirrors one W6-D2 group.scan_selections entry (filearr.agent_config
// .ScanSelection): a preset name + explicit path specs + regex refinements, gated
// by `enabled` (default true).
type scanSelection struct {
	Preset       string   `json:"preset"`
	Paths        []string `json:"paths"`
	IncludeRegex []string `json:"include_regex"`
	ExcludeRegex []string `json:"exclude_regex"`
	Enabled      *bool    `json:"enabled"`
}

func (s scanSelection) enabled() bool { return s.Enabled == nil || *s.Enabled }

// ScanRootResult is the resolved scan-root set for a group's scan_selections: the
// deduped roots, whether the fan-out cap truncated them, per-spec expansion
// errors, and how many enabled selections were consumed.
type ScanRootResult struct {
	Roots           []string          `json:"roots"`
	Truncated       bool              `json:"truncated"`
	Errors          map[string]string `json:"errors,omitempty"`
	SelectionsCount int               `json:"selections"`
}

// ExpandScanSelections resolves the W6-D2 `group.scan_selections` carried in a raw
// policy body into the effective scan-root set, using the SAME pathspec engine the
// inventory command uses (preset resolution → env/glob expansion → fan-out cap).
//
// THIS IS THE POLICY→ROOTS CONSUMPTION SEAM (W6-D3): the roots are returned for
// the caller to LOG and PERSIST, but NO scan is started from them this round.
// Auto-starting a scan from a group policy is a deliberate follow-up (it needs the
// scheduler/cancellation coordination the scan path already owns); wiring the
// resolved roots here proves the policy vocabulary resolves end-to-end without
// taking that action. Best-effort: a malformed body yields an empty result.
func ExpandScanSelections(host pathspec.Host, rawPolicy []byte) ScanRootResult {
	var doc struct {
		Group struct {
			ScanSelections []scanSelection `json:"scan_selections"`
		} `json:"group"`
	}
	if len(rawPolicy) == 0 || json.Unmarshal(rawPolicy, &doc) != nil {
		return ScanRootResult{}
	}
	exp := &pathspec.Expander{}
	seen := map[string]bool{}
	out := ScanRootResult{}
	for _, sel := range doc.Group.ScanSelections {
		if !sel.enabled() {
			continue
		}
		out.SelectionsCount++
		var specs []string
		if sel.Preset != "" {
			if ps, err := pathspec.ResolvePreset(host, sel.Preset); err == nil {
				specs = append(specs, ps...)
			} else {
				addErr(&out, sel.Preset, err.Error())
			}
		}
		specs = append(specs, sel.Paths...)
		res := exp.Expand(specs)
		if res.Truncated {
			out.Truncated = true
		}
		for spec, msg := range res.Errors {
			addErr(&out, spec, msg)
		}
		for _, r := range res.Roots {
			if !seen[r] {
				seen[r] = true
				out.Roots = append(out.Roots, r)
			}
		}
	}
	return out
}

func addErr(r *ScanRootResult, key, msg string) {
	if r.Errors == nil {
		r.Errors = map[string]string{}
	}
	r.Errors[key] = msg
}
