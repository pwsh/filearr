// Package permissions is the SCAFFOLD for the W7 full-ACE `permissions`
// collector (docs/research/permissions-enumeration-audit.md). It emits the FULL
// normalized ACE list (owner + every allow/deny entry, native mask preserved
// verbatim, inheritance/scope flags) — distinct from, and superseding, the
// existing summary-only `perms`/`owner` collectors in the parent package.
//
// SCAFFOLD STATUS — read before extending:
//   - The pure, OS-independent cores are IMPLEMENTED FOR REAL and unit-tested:
//     the normalized record schema (record.go), the native-mask→verb mapping
//     tables (masks.go), well-known-principal classification (wellknown.go), the
//     POSIX ACL xattr binary decoder (posixacl.go), and mount-fidelity detection
//     (fidelity.go).
//   - Every OS-I/O boundary is an INERT stub returning ErrPermissionsScaffold
//     (collector.go + permissions_{windows,posix,darwin,other}.go). No real
//     syscall, no exec, no CGO is issued yet — each stub carries a
//     TODO(W7-Tn) naming the exact API the real read will use.
//   - The collector is deliberately NOT added to
//     inventory.DefaultRegistry() and therefore NOT advertised in
//     Capabilities(). Central must never offer or run it until the per-OS reads
//     land. See the "W7: register in DefaultRegistry once the per-OS reads are
//     implemented" seam in collector.go.
//
// Open questions the architect must resolve before greenlight are catalogued in
// the brief's §9.1 (storage shape, central-scanner parity, exclusion escape
// hatch, SACL privilege model, Samba share-ACL, drift-retention default).
package permissions
