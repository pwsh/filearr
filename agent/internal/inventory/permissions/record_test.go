package permissions

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestRecordJSONRoundTrip(t *testing.T) {
	orig := Record{
		ItemRef:     "lib-7:movies/a.mkv",
		CollectedAt: time.Date(2026, 7, 18, 12, 0, 0, 0, time.UTC),
		Owner: Principal{
			Kind: KindUser, ID: "S-1-5-21-111-222-333-1001",
			Name: "CORP\\jsmith", WellKnown: "",
		},
		Group: &Principal{Kind: KindGroup, ID: "S-1-5-21-111-222-333-513", Name: "CORP\\Domain Users"},
		Posture: Posture{
			DaclPresent: true, DaclCanonical: true, GenericMappingApplied: true,
		},
		Fidelity: FidelityFullNative,
		Entries: []ACE{
			{
				Principal:        Principal{Kind: KindWellKnown, ID: "S-1-5-18", WellKnown: WKSystem},
				Type:             TypeAllow,
				Verbs:            []string{VerbFull},
				RawMask:          "0x1f01ff",
				Inherited:        true,
				ContainerInherit: true,
				ObjectInherit:    true,
				Scope:            ScopeSubtree,
				Source:           SourceLocal,
				OrderIndex:       0,
			},
			{
				Principal:  Principal{Kind: KindUser, ID: "S-1-5-21-111-222-333-1001", Name: "CORP\\jsmith"},
				Type:       TypeDeny,
				Verbs:      []string{VerbWrite, VerbDelete},
				RawMask:    "0x10002",
				Scope:      ScopeThis,
				Source:     SourceLocal,
				OrderIndex: 1,
			},
		},
	}

	b1, err := json.Marshal(orig)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got Record
	if err := json.Unmarshal(b1, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	// Round-trip stability: re-marshalling the decoded value reproduces the bytes.
	b2, err := json.Marshal(got)
	if err != nil {
		t.Fatalf("re-marshal: %v", err)
	}
	if string(b1) != string(b2) {
		t.Fatalf("round-trip mismatch:\n%s\n%s", b1, b2)
	}

	// Spot-check load-bearing fields survived.
	if got.Group == nil || got.Group.ID != "S-1-5-21-111-222-333-513" {
		t.Fatalf("group lost: %+v", got.Group)
	}
	if !got.CollectedAt.Equal(orig.CollectedAt) {
		t.Fatalf("collected_at: %v want %v", got.CollectedAt, orig.CollectedAt)
	}
	if len(got.Entries) != 2 || got.Entries[1].Type != TypeDeny {
		t.Fatalf("entries lost: %+v", got.Entries)
	}
	if got.Entries[0].OrderIndex != 0 || got.Entries[1].OrderIndex != 1 {
		t.Fatalf("order_index not preserved: %+v", got.Entries)
	}
}

func TestRecordOmitsEmptyGroupAndOptionalAceFlags(t *testing.T) {
	r := Record{
		CollectedAt: time.Date(2026, 7, 18, 0, 0, 0, 0, time.UTC),
		Owner:       Principal{Kind: KindUser, ID: "1000"},
		Fidelity:    FidelityPosixModeOnly,
		Entries: []ACE{{
			Principal: Principal{Kind: KindUser, ID: "user_obj"},
			Type:      TypeAllow, Verbs: []string{VerbRead}, RawMask: "tag=0x01,perm=04",
			Scope: ScopeThis, Source: SourceLocal,
		}},
	}
	b, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	s := string(b)
	// Optional/absent fields must be omitted, not emitted as null/false noise.
	for _, absent := range []string{`"group"`, `"container_inherit"`, `"object_inherit"`, `"item_ref"`} {
		if strings.Contains(s, absent) {
			t.Fatalf("expected %s omitted, got: %s", absent, s)
		}
	}
}
