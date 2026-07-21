package permissions

import "time"

// Principal identity kinds (brief §3.1: owner.kind).
const (
	KindUser      = "user"
	KindGroup     = "group"
	KindWellKnown = "well_known"
	KindUnmapped  = "unmapped"
)

// ACE decision types. POSIX.1e ACLs are additive-only (never TypeDeny); Windows,
// NFSv4 and macOS extended ACLs carry both (brief §1.1/§1.3/§2.2, §3.2).
const (
	TypeAllow = "allow"
	TypeDeny  = "deny"
)

// ACE scope: whether an entry governs THIS object, its SUBTREE (inheritable), or
// is a POSIX default ACL describing who newly-created children INHERIT (brief §9
// — captured as a SEPARATE list, never conflated with today's access ACL).
const (
	ScopeThis       = "this"
	ScopeSubtree    = "subtree"
	ScopeDirDefault = "dir_default"
)

// ACE source layer: the file/folder ACL vs the share-level ACL. Effective access
// is the intersection of the two and is NEVER blended in v1 (brief §2.1/§3.5).
const (
	SourceLocal = "local"
	SourceShare = "share"
)

// Record.Fidelity — how much the reported ACL can be trusted for this entry
// (brief §3.1). Distinct from MountFidelity (fidelity.go), which is the raw
// per-mount classification this value is derived from.
const (
	FidelityFullNative          = "full_native"
	FidelitySynthesizedFromMode = "synthesized_from_mode"
	FidelityPosixModeOnly       = "posix_mode_only"
	FidelityUnavailable         = "unavailable"
)

// Principal is one identity referenced by an owner/group field or an ACE. The
// raw source identifier (ID) is ALWAYS preserved verbatim; Name is best-effort
// resolution (omitted when unresolved); WellKnown is the stable classification
// tag (wellknown.go) the central exclusion filter keys on (brief §2.4/§4).
type Principal struct {
	Kind      string `json:"kind"`                 // KindUser | KindGroup | KindWellKnown | KindUnmapped
	ID        string `json:"id"`                   // raw SID / uid|gid / name@domain, verbatim, always present
	Name      string `json:"name,omitempty"`       // best-effort resolved display name
	WellKnown string `json:"well_known,omitempty"` // stable tag when recognized (e.g. "SYSTEM"), else empty
}

// ACE is one normalized access control entry. RawMask is ALWAYS the verbatim
// native mask (hex for NTFS/NFSv4, octal+tag for POSIX) so a forensic consumer
// can recover the original grant even where the verb mapping is lossy or
// partial. OrderIndex preserves raw storage order and MUST NOT be re-sorted —
// Windows DACL evaluation is order-dependent (brief §9).
type ACE struct {
	Principal        Principal `json:"principal"`
	Type             string    `json:"type"`  // TypeAllow | TypeDeny
	Verbs            []string  `json:"verbs"` // normalized verb set (§3.2); RawMask is the source of truth
	RawMask          string    `json:"raw_mask"`
	Inherited        bool      `json:"inherited"`
	ContainerInherit bool      `json:"container_inherit,omitempty"`
	ObjectInherit    bool      `json:"object_inherit,omitempty"`
	NoPropagate      bool      `json:"no_propagate,omitempty"`
	InheritOnly      bool      `json:"inherit_only,omitempty"`
	Scope            string    `json:"scope"`  // ScopeThis | ScopeSubtree | ScopeDirDefault
	Source           string    `json:"source"` // SourceLocal | SourceShare
	OrderIndex       int       `json:"order_index"`
}

// Posture is the object-level security-descriptor posture (brief §9). All three
// are meaningful only for NTFS; POSIX records leave them at their zero values.
type Posture struct {
	DaclPresent           bool `json:"dacl_present"`
	DaclCanonical         bool `json:"dacl_canonical"`
	GenericMappingApplied bool `json:"generic_mapping_applied"`
}

// Record is one collected path's full permission snapshot: owner + optional
// group + the ordered ACE list + the fidelity/posture context. It is the shape
// the (future) real per-OS reads populate and the collector emits into the
// inventory walk's per-entry map (brief §3.1).
type Record struct {
	ItemRef     string     `json:"item_ref,omitempty"` // best-effort catalog link; usually absent for a bare inventory root
	CollectedAt time.Time  `json:"collected_at"`
	Owner       Principal  `json:"owner"`
	Group       *Principal `json:"group,omitempty"` // POSIX group_obj; nil for Windows (owner group is separate/optional)
	Posture     Posture    `json:"posture"`
	Fidelity    string     `json:"fidelity"` // Fidelity* constants
	Entries     []ACE      `json:"entries"`
}
