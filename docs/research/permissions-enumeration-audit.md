# Research Brief — Permissions Enumeration, Reconciliation & Audit (W7-R1)

Scope: how the distributed Go agent + central should enumerate, reconcile,
report, and (optionally) audit **file and folder permissions** across local
filesystems and network shares, on Windows/Linux/macOS. This is a research
brief only — no production code, no commits. Researched 2026-07-18 against
current OS vendor docs, `agent/internal/inventory/` (the W6-D3 Collector
framework this work extends, NOT redesigns), `backend/filearr/agent_config.py`
(`InventoryConfig`), `backend/filearr/api/agent_inventory.py` (the
inventory-results channel), `backend/filearr/reports.py`/`alerts/` (the
canned-report and alert-pipeline machinery this proposes to plug into), and
`docs/research/agent-inventory-presets.md` (W6-R1 — the OneDrive/XDG/TCC
gotchas a permissions collector inherits verbatim).

---

## 0. tl;dr

Filearr already ships two relevant collectors (`agent/internal/inventory/`):
`owner` (POSIX uid/gid + name; Windows owner SID) and `perms` (POSIX 4-digit
octal + xattr NAME list; Windows DACL summary — ACE count + compact
per-trustee rights string). Both are deliberately **summary-only**, opt-in via
being named in a config group's `inventory.collectors` list. This brief
proposes a **new, separate `permissions` collector** — distinct from the
existing summary `perms` — that emits the FULL normalized ACE list (owner +
every allow/deny entry, native mask preserved verbatim, inheritance/scope
flags), still fail-soft and metadata-only, still riding the existing
inline/NDJSON inventory-results channel unchanged.

The three hardest findings, in order of how much they'd corrupt a permissions
report if missed:

1. **Cross-host principal identity has no universal key.** A Windows SID, a
   POSIX uid, an NFSv4 `user@domain` string, and an AD account may all denote
   the *same* human, or may coincidentally collide in number/name across
   *unrelated* hosts. Only a domain-joined AD SID is a reliable cross-host
   key; a local SID or a bare uid is host-scoped and must be reported as such,
   never silently treated as globally unique. This is the crux of "reconcile
   local and network share permissions" (§2.4) and the single hardest problem
   in this brief.
2. **A mounted network share can silently lose ACL fidelity.** A Linux `cifs`
   mount without the `cifsacl` option shows *synthesized* mode bits derived
   from mount options (`uid=`, `gid=`, `file_mode=`, `dir_mode=`) — NOT the
   real NTFS ACL on the server. An agent reading permissions through such a
   mount must know and report which fidelity it got (§2.1, §6) or the report
   is actively misleading, not just incomplete.
3. **Reading POSIX ACLs and macOS extended ACLs has no pure-Go path.** Both
   require either CGO bindings to the platform's native `acl(3)`/`libacl`
   library or shelling out to `getfacl`/`ls -le`. Windows, by contrast, is
   fully coverable with the `golang.org/x/sys/windows` calls already in use in
   `perms_windows.go`/`owner_windows.go` — no CGO, no shell-out. This asymmetry
   should be resolved as its own spike before implementation (§7, W7-T2a),
   mirroring the `phase-5-t3a-gitignore-spike.md` precedent in this repo.

Recommended v1 scope (detail in §8): the `permissions` collector (raw
normalized ACEs + owner, no effective-access computation) + the normalized
record schema (§3.1) + a new additive `permission_snapshots` table (§3.3) + a
basic "permissions" canned report (§3.4), opt-in via a new
`inventory.permissions` config-group block (§4), defaulting to **exclude
well-known principals and inherited ACEs** so a report highlights only
explicit, meaningful grants. Effective-access computation and change-audit/
alerting are fast-follows.

---

## 1. Per-OS permission models + enumeration APIs

### 1.1 POSIX (Linux/macOS local)

**Mode bits + owner** are free — already read by `os.Lstat`/`syscall.Stat_t`
in the existing `owner_posix.go`/`perms_posix.go` collectors (`Uid`, `Gid`,
`Mode`, low 12 bits = permission + setuid/setgid/sticky). No change needed
here; the new `permissions` collector reuses this, it does not re-stat.

**POSIX.1e ACLs** (the Linux/FreeBSD "POSIX draft" ACL model, additive-only —
they can grant beyond the mode bits but never explicitly deny) are stored as
the `system.posix_acl_access` (effective ACL) and `system.posix_acl_default`
(inherited-by-new-children ACL, directories only) extended attributes.
[`acl(5)`](https://www.man7.org/linux/man-pages/man5/acl.5.html) documents the
logical entry shape (tag type: `user`/`group`/`user_obj`/`group_obj`/`other`/
`mask`, optional qualifier id, rwx permission triplet) but the **on-disk xattr
encoding is a private kernel binary format**, not part of the stable man-page
contract — `getfattr`'s raw dump shows it as an opaque base64 blob, and the
authoritative decoder is `libacl`'s C source, not a documented byte layout a
Go program should hand-parse. Two read strategies exist:

- **`getxattr`/`Lgetxattr` the raw bytes** (via `golang.org/x/sys/unix`,
  already used for the xattr NAME listing in the existing `perms` collector)
  and hand-decode the private format — brittle, undocumented, NOT
  recommended.
- **CGO binding to `libacl`'s `acl_get_file`/`acl_to_text`**, or **shell out
  to `getfacl`**. Confirmed via the existing Go ACL library landscape
  ([naegelejd/go-acl](https://pkg.go.dev/github.com/naegelejd/go-acl),
  [joshlf/go-acl](https://pkg.go.dev/github.com/joshlf/go-acl)) — every
  general-purpose Go ACL library found binds to the native `acl(3)` library
  via CGO; none parse the xattr bytes in pure Go. This is the recommended
  path, gated on whether the agent's current build/release pipeline is
  pure-Go (cross-compilation matrix) — flagged as an open spike question,
  §7 W7-T2a.
- **xattr NAMES only** (what the existing `perms` collector already does) is
  a cheap, zero-new-syscall-type signal that ACLs/SELinux/capabilities/quota
  *might* be present (their presence surfaces as `system.posix_acl_access`,
  `security.selinux`, `security.capability` xattr *names* in the existing
  list) without paying the ACL-decode cost — useful as a fast pre-filter but
  not a substitute for the real collector.

**Linux security xattrs**: `security.selinux` (SELinux label string,
human-readable, cheap once matched — a plain `getxattr` for that one name,
not the ACL decode path) and `security.capability` (Linux file capabilities,
binary `vfs_cap_data` struct — same "needs a real decoder, not ad hoc" caveat
as ACLs, lower priority for v1). Both already surface as bare names in the
existing `perms` collector's xattr list; decoding their *values* is new,
separate, opt-in work, not core to the ACE-list schema below (they aren't
"rights grants" in the ACE sense — flagged for a future collector, out of
scope for W7 v1).

**Enumeration cost**: every ACL/xattr-value read is a *separate syscall per
file*, on top of the `Lstat` the walk already pays for free — confirmed by
the existing `perms` collector's own doc comment ("a SEPARATE syscall per
file... opt-in territory"). This scales the same concern the W6-R1 brief
already flagged for xattr NAME listing, now for xattr VALUE decoding, which
is strictly more expensive (variable-length parse, not just a name string).

### 1.2 Windows (local NTFS/ReFS)

The full security descriptor — owner, primary group, DACL, SACL — is queried
via
[`GetNamedSecurityInfo`](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getnamedsecurityinfow)
(already the exact call in `owner_windows.go`/`perms_windows.go`), which
**works identically on local paths and UNC paths** (`\\server\share\path`) —
confirmed by Microsoft's own reference: "You can use the `GetNamedSecurityInfo`
function with local or remote files or directories on an NTFS file system."
This is the key fact that makes a Windows agent's file-level ACL read
transparent whether the target is a local disk or a mounted network share —
no separate "remote ACL" API is needed at the file level (§2.1 covers the
distinct *share-level* ACL, which IS a separate call).

- **DACL** (discretionary — the "normal" allow/deny ACEs): readable by any
  principal with `READ_CONTROL` on the object, or the object's owner — no
  special privilege for the common case. This is what the existing
  `perms_windows.go` already reads (`DACL_SECURITY_INFORMATION`).
- **SACL** (system audit ACEs — who gets logged accessing what): reading OR
  writing a SACL requires the `SE_SECURITY_NAME` privilege
  (`SeSecurityPrivilege`) to be *enabled* in the calling process's token —
  by default held only by the local `Administrators` group. An agent running
  as a non-elevated service account will get a clean, well-defined access
  failure attempting `SACL_SECURITY_INFORMATION` — this must be a reported
  **health state** ("SACL unavailable: insufficient privilege"), never a
  silent empty result, mirroring the W6-R1 TCC precedent exactly.
- **Inheritance flags** per ACE: `INHERITED_ACE` (this ACE flowed down from a
  parent, vs explicitly set here), `OBJECT_INHERIT`/`CONTAINER_INHERIT` (will
  this ACE propagate to child files/subfolders), `INHERIT_ONLY_ACE`,
  `NO_PROPAGATE_INHERIT_ACE`. All are bits on `ACE_HEADER.AceFlags`, already
  reachable from the same `windows.GetAce` call the existing collector uses —
  the existing collector reads `ace.Header.AceType` but not yet
  `ace.Header.AceFlags`; extending to read flags is additive, no new API.
- **SID resolution**: `LookupAccountSid` (already used, as `.LookupAccount("")`
  on the SID in both existing collectors) resolves a SID to `domain\account`
  — a potential domain-controller round trip on a domain-joined machine, so
  it stays best-effort/lazy per the existing code's own comment
  ("collecting raw SIDs by default... resolving to names lazily", W6-R1 §6).
- **Well-known SIDs**: per [Microsoft's reference table](https://learn.microsoft.com/en-us/windows/win32/secauthz/well-known-sids),
  fixed, OS-version-independent values exist for `Everyone` (`S-1-1-0`),
  `CREATOR OWNER` (`S-1-3-0`), `SYSTEM`/`NT AUTHORITY\SYSTEM` (`S-1-5-18`),
  `Administrators` (`S-1-5-32-544`), `Users` (`S-1-5-32-545`), and dozens
  more. These are recognizable by a **string-prefix match against a static
  table**, not a lookup — feeds directly into the "exclude base permissions"
  requirement (§4).
- **Full-fidelity raw form**: `ConvertSecurityDescriptorToStringSecurityDescriptor`
  produces the canonical SDDL string. The existing collectors deliberately do
  NOT do this (compact summary only, per the code's own comment "NOT a full
  SDDL dump"). This brief's normalized-ACE schema (§3.1) supersedes that
  constraint for the *new* `permissions` collector (full ACE list is the
  point), but still does not propose emitting raw SDDL by default — the
  normalized record plus the verbatim native mask per ACE is the "raw enough"
  form; a full SDDL string is available as an optional `raw_native` field
  (§3.3) for forensic drill-down, not the primary shape.

### 1.3 macOS specifics

macOS supports **NFSv4-style extended ACLs** (`ACL_TYPE_EXTENDED`), not the
Linux POSIX.1e draft — a materially different model: NFSv4-style ACEs are
**ordered** and support explicit **allow and deny** entries (POSIX.1e ACLs
are additive-only, never deny) — confirmed via
[the SambaWiki NFS4_ACL_overview](https://wiki.samba.org/index.php/NFS4_ACL_overview)
and corroborating community references. Viewed via `ls -le` (shows entries
like `user:tuxi deny read,write` after the standard POSIX permission string,
with an `@` suffix on the mode string flagging extended-attribute/ACL
presence) or programmatically via `acl_get_file(path, ACL_TYPE_EXTENDED)` from
the same `acl(3)` library family used on Linux — same CGO-or-shell-out
tradeoff as §1.1, and per the existing `naegelejd/go-acl` library survey,
that library already claims to bind macOS's ACL support through the same
`acl(3)` interface, worth evaluating directly rather than writing a fresh
binding.

**Interaction with POSIX mode**: ACEs are evaluated in order; a `deny` entry
that matches the requesting principal for the requested right blocks access
even if a later (or the underlying POSIX mode) would allow it; if no ACE
decides the request, the standard POSIX mode bits apply as the fallback. This
mirrors NFSv4/Windows-style "first match wins, ACL overrides mode" semantics,
not POSIX.1e's "union of all applicable grants" semantics — the mapping table
in §3.2 must NOT conflate the two ACL flavors' evaluation order even though
their v1 *reporting* (raw ACE dump) doesn't need to *compute* precedence.

**BSD flags** (`chflags` — `UF_IMMUTABLE`/`SF_IMMUTABLE` aka `uchg`/`schg`,
append-only `UF_APPEND`/`SF_APPEND`) sit entirely outside both the mode-bit
and ACL frameworks — a file can be fully "writable" by every ACE and mode bit
and still be un-writable because `schg` is set. These must be surfaced as a
**separate small field**, not folded into the ACE rights list (they are not a
principal-scoped grant/deny, they are a whole-object flag). Read via
`stat`/`lstat`'s `st_flags` — per the W6-R1 brief's own finding, `x/sys/unix`
does not reliably expose `Flags` on Darwin's `Stat_t` today, so this needs the
same small CGO/raw-syscall shim already flagged as a spike candidate there —
this brief reuses that spike rather than opening a second one.

TCC/Full Disk Access gating (readability of `~/Documents` etc. without an
explicit FDA grant) is inherited unchanged from W6-R1 §3.4 — a permissions
collector under a TCC-protected root fails exactly the same way a content
listing does, and must use the same "0 items, FDA suspected" vs "0 items,
verified empty" distinction already designed there.

---

## 2. Network share permissions

This is the hard, differentiating part: what a distributed agent actually
*sees* when it walks a network-mounted path depends heavily on the mount
method, not just the underlying protocol.

### 2.1 SMB/CIFS

**Two layers of permission exist and are evaluated independently**: the
**share-level ACL** (who may connect to `\\server\share` at all, and with
what maximum access) and the **filesystem ACL** on the actual files/folders
under that share (NTFS ACLs on Windows/ReFS servers, or POSIX+xattr-ACL on a
Samba server). **Effective access is the intersection of the two** — a
principal granted Full Control on the share but Read-only on the NTFS ACL
gets Read; the reverse is equally true. This is explicitly documented
Microsoft troubleshooting guidance: "the effective permission is never more
permissive than either individual permission set." A permissions report that
only captures one layer is incomplete by construction — both must be
represented, tagged by `source: local|share` in the normalized schema (§3.1),
and never silently merged into one blended verdict in v1 (that blending IS
effective-access computation, explicitly deferred to v2, §3.5).

**Share-level ACL enumeration**: `Get-SmbShareAccess` (PowerShell, Windows
management-plane only — not usable from a headless Go agent binary without
shelling to PowerShell, undesirable) vs the Win32 `NetShareGetInfo`/
`NetrShareGetInfo` RPC at **information level 502**, which returns a
`SHARE_INFO_502` structure including the share's own security descriptor —
this is callable directly from Go via `netapi32.dll` (same pattern already
used in this codebase for defining constants the `x/sys/windows` package
doesn't name, per the OneDrive reparse-tag precedent in W6-R1 §1.2). This is
proposed as an **optional, Windows-only enrichment call**, not core v1 (most
agents are share *consumers*, not the server hosting the share; the
file-level ACL via `GetNamedSecurityInfo` on the UNC path already covers the
majority use case, §1.2).

**Samba server side** (relevant when the network share the agent walks is
served by Samba, not Windows Server): the
[`vfs_acl_xattr`](https://www.samba.org/samba/docs/current/man-html/vfs_acl_xattr.8.html)
VFS module stores the real NT ACL (SIDs and all) verbatim in a
`security.NTACL` extended attribute — when present, Samba serves the true
Windows-shaped ACL over SMB regardless of the underlying POSIX filesystem's
own permission model. When `security.NTACL` is *absent* on a given file,
Samba **synthesizes** an ACL from POSIX mode bits per the module's
`acl_xattr:default acl style` setting (`posix` default: owner/group/other
mapped to ACEs plus a `SYSTEM` full-control ACE; `windows`: owner +
`SYSTEM` only; `everyone`: full control to `S-1-1-0`). A permissions
collector talking to a Samba share over SMB cannot distinguish a genuine
"minimal ACL" from a synthesized fallback from the wire protocol alone — this
is a server-side fidelity question the agent has no visibility into and
should not claim to resolve; flagged in §6 as a documented gap, not solved
here.

**Linux `cifs` mount client-side behavior** — the single highest-impact
fidelity hazard in this brief: per the
[kernel CIFS admin guide](https://docs.kernel.org/admin-guide/cifs/usage.html)
and the [sambaXP mapping talk](https://sambaxp.org/fileadmin/user_upload/sambaxp2021-slides/Prasad_Access_control_and_IDmapping.pdf),
a plain `mount.cifs` **without** the `cifsacl` option does NOT expose the
server's real ACL at all — the client instead **synthesizes** uid/gid/mode
from static mount options (`uid=`, `gid=`, `file_mode=`, `dir_mode=`,
`noperm`), meaning every file under that mount reports the *same* fabricated
owner/mode regardless of what the server's actual ACL says. With `cifsacl`
set, the client instead maps CIFS/NTFS ACLs to/from POSIX bits and maps SIDs
to/from local uid/gid via `cifs.idmap` (working with Samba `winbind`, or
since cifs-utils 5.9 a pluggable backend including an SSSD plugin) — this
is the mode that actually surfaces real per-file ACL data to a Linux agent. A
third mode, `idsfromsid`/`modesfromsid`, is a **client-enforced, Linux-only**
scheme (encodes uid/gid/mode into reserved SIDs client-side) intended for
Linux-only-client environments, not a real cross-platform ACL projection.
**Design implication**: the agent MUST detect which of these three postures a
given `cifs` mount is in (read `/proc/mounts` for the mount options string —
cheap, one read per distinct mount point, cacheable) and stamp every entry
under that mount with the corresponding `fidelity` value (§3.1) — silently
reporting synthesized mode bits as if they were the real ACL is the single
most misleading failure mode this brief can produce.

### 2.2 NFS

**NFSv3** carries **mode bits only** — no ACL concept in the base protocol —
and trusts the numeric uid/gid asserted by the client under `AUTH_SYS`
(effectively no real cross-host identity verification; a malicious or
misconfigured client can claim any uid). Some vendors (Solaris, some Linux
distributions) layered a side-channel `NFSACL` protocol extension for
POSIX-ACL-over-NFSv3, but it is non-standard and not universally supported —
not proposed as a v1 target.

**NFSv4** has **real ACLs**, deliberately modeled as "a superset of NTFS
ACLs" — richer than POSIX.1e, ordered allow/deny semantics like Windows/macOS
(not POSIX.1e's additive-only model), read via `nfs4_getfacl`
([Linux NFS wiki](https://wiki.linux-nfs.org/wiki/index.php/ACLs)). NFSv4
also changed identity representation: **principals are sent as `name@domain`
strings over the wire**, not raw numeric uid/gid (NFSv3's model) — resolved
locally via `idmapd`, which maps between the wire string and local uid/gid.
This is a second, independent identity-mapping layer alongside SMB's SID
mapping (§2.1) — the two protocols solve "which principal is this" in
completely different, non-interoperable ways, reinforcing why principal
canonicalization (§2.4) needs a protocol-aware fallback chain rather than one
universal key.

Linux does **not** have upstream kernel support for exposing NFSv4-shaped
ACLs as a first-class local ACL type (the RichACL kernel effort never landed
upstream) — on the *server* side, Linux NFSv4 servers typically still map
down to POSIX ACLs internally per the Linux NFS wiki. This means a Linux
agent reading via `nfs4_getfacl` against a non-Linux NFSv4 server (a NetApp,
TrueNAS, illumos-based box) may see richer ACLs than the same host's *local*
POSIX ACL tooling can natively represent — another argument for a normalized
schema (§3.1) that doesn't assume POSIX is the lowest common denominator.

### 2.3 Mounted-share visibility summary

| Access method | What the agent actually sees | Fidelity |
|---|---|---|
| Windows agent, UNC path (`\\server\share\...`), any server | Real file-level NTFS ACL via `GetNamedSecurityInfo` (works transparently over SMB) | Full native |
| Linux agent, `cifs` mount, `cifsacl` set | Real NTFS ACL, SID-mapped to POSIX via `cifs.idmap`/winbind/SSSD | Full native (mapped) |
| Linux agent, `cifs` mount, no `cifsacl` | Fabricated mode/uid/gid from mount options — same for every file | **Synthesized — do not trust** |
| Linux agent, Samba share, server-side `vfs_acl_xattr` present | Real NT ACL as stored server-side | Full native (server-dependent) |
| Linux agent, Samba share, no `security.NTACL` xattr on the file | Server-synthesized ACL from POSIX mode (`posix`/`windows`/`everyone` style) | Synthesized (server-side, agent can't tell) |
| Any agent, NFSv3 mount | Mode bits + client-asserted uid/gid only, no ACL | POSIX-mode-only, weak identity trust |
| Any agent, NFSv4 mount | Real NFSv4 ACL, `name@domain` principals via `idmapd` | Full native |

### 2.4 The identity-reconciliation core problem

**This is the hardest technical problem in this brief.** The same human
principal can appear as:

- a Windows SID (`S-1-5-21-<domain>-<rid>` if AD-joined, `S-1-5-21-<machine>-<rid>`
  if local-only — the domain-vs-local distinction is encoded IN the SID
  structure itself, which is useful),
- a POSIX uid on the NAS/file server (possibly winbind-mapped from the same
  AD SID via a deterministic algorithm, possibly a wholly separate local
  account),
- an NFSv4 `name@domain` string,
- a local (non-AD) account on the agent's own host, with no relationship to
  any of the above.

**Canonicalization strategy, strongest to weakest signal:**

1. **Domain-joined AD SID string** (`S-1-5-21-<domain-id>-<rid>`, recognizable
   by the fixed `S-1-5-21-` prefix followed by a domain identifier that
   matches the environment's known AD domain) is the strongest cross-host
   key — the same domain SID denotes the same account whether observed on a
   Windows client, a Samba server (winbind-joined to the same domain), or
   another Windows Server. This is the one case where "the same principal
   observed from two hosts" can be asserted with confidence.
2. **NFSv4 `name@domain` string**, when the domain matches a known
   authentication realm, is a comparably strong signal for NFSv4-only
   environments (no Windows/SMB involved).
3. **Local SID or bare uid/gid**, MUST be treated as **host-scoped, not
   globally unique** — a local `S-1-5-21-<machine-id>-1000` or a bare
   `uid=1000` on one NAS has zero relationship to `uid=1000` on a different
   NAS or on the agent's own host; they may denote entirely different
   people. The canonicalization scheme must qualify these with the
   originating host (agent hostname, or the share's server identity if
   knowable) — e.g. `local:<hostname>:<sid-or-uid>` — rather than storing
   the bare id as if it were portable.
4. **Name-only fallback** (a bare `DOMAIN\user` or username string with no
   resolvable SID/uid behind it — can happen when `LookupAccountSid`/
   `getpwuid` fails) is the weakest signal, prone to typo/homoglyph
   collision, used only when nothing stronger resolved.
5. **Unmappable principal** (an orphaned SID with no matching AD/local
   account — e.g. a deleted domain user still referenced in an old ACE — or a
   uid with no passwd entry, e.g. a container-mapped or NFS-realm uid) MUST
   be reported as its raw identifier with an explicit `resolved: false` flag,
   **never dropped silently**. An orphaned/unmappable principal in an ACE is
   frequently exactly the kind of finding an admin permissions audit exists
   to surface (stale grants, drifted identity mapping) — silently omitting it
   would defeat the report's purpose.

No fully general solution exists for reconciling an unqualified local id
across hosts with no shared identity provider — this is a fundamental
limitation of the underlying OS/protocol designs, not a gap this brief's
design can close. The schema (§3.1) is built to represent the ambiguity
honestly (kind + confidence-ordered canonical_id + raw source_identifier)
rather than to force a false resolution.

---

## 3. Reconciliation + reporting model

### 3.1 Normalized permission-record schema

One record per **ACE** (access control entry), plus a separate owner field
per collected path. Emitted by the new `permissions` collector as part of
the same per-entry map the existing `inventory` walk already produces
(`{path, rel, ...collector fields}` — see `agent/internal/inventory/run.go`'s
`walkState.walkRoot`), so no change to the walk, transport, or inline/NDJSON
encoding decision (`agent/internal/inventory/run.go`'s `encodeResult`) is
needed — this is purely a new collector's output shape.

```jsonc
{
  "owner": {                       // one per entry
    "kind": "user",                // user | group | well_known | unmapped
    "canonical_id": "S-1-5-21-...-1001",   // strongest-available id, §2.4
    "source_identifier": "S-1-5-21-...-1001", // raw, verbatim, always present
    "display": "CORP\\jsmith",     // best-effort resolved name, omitted if unresolved
    "domain": "CORP",              // omitted for local/unscoped ids
    "resolved": true
  },
  "aces": [
    {
      "principal": { /* same shape as owner */ },
      "allow": true,               // false = deny (Windows/NFSv4/macOS only; POSIX ACLs are never deny)
      "rights": ["read", "write", "list"],   // normalized verb set, see §3.2
      "native_mask": "0x1200a9",   // verbatim: hex mask (Windows/NFSv4) or octal+tag (POSIX)
      "native_kind": "ntfs",       // ntfs | posix_acl | posix_mode | nfsv4 | macos_acl
      "inherited": false,
      "inherit_flags": ["container_inherit"],  // omitted where not applicable
      "scope": "subtree"           // this | subtree, derived from inherit flags / POSIX default-vs-access ACL
    }
  ],
  "fidelity": "full_native",       // full_native | synthesized_from_mode | posix_mode_only | unavailable
  "raw_native": null                // optional, capped-size verbatim dump (SDDL / getfacl text / nfs4_getfacl text) — opt-in, off by default
}
```

### 3.2 Native-mask → normalized-verb mapping tables

Normalized verb set: `read, write, execute, append, delete, delete_child,
list, read_attr, write_attr, read_perms, change_perms, take_own, full`.

**Windows NTFS mask → verbs** (bit → verb; `GENERIC_ALL`/`FILE_GENERIC_*`
already partially bucketed in the existing `perms_windows.go`
`compactRights`, extended here to the full verb set rather than a compact
mnemonic string):

| NTFS bit | Verb |
|---|---|
| `FILE_READ_DATA` | `read` |
| `FILE_WRITE_DATA` | `write` |
| `FILE_APPEND_DATA` | `append` |
| `FILE_EXECUTE` | `execute` |
| `FILE_LIST_DIRECTORY` (dir alias of `FILE_READ_DATA`) | `list` |
| `FILE_DELETE_CHILD` | `delete_child` |
| `DELETE` | `delete` |
| `READ_CONTROL` | `read_perms` |
| `WRITE_DAC` | `change_perms` |
| `WRITE_OWNER` | `take_own` |
| `FILE_READ_ATTRIBUTES`/`FILE_READ_EA` | `read_attr` |
| `FILE_WRITE_ATTRIBUTES`/`FILE_WRITE_EA` | `write_attr` |
| `GENERIC_ALL` | `full` (expands to all of the above) |
| `SYNCHRONIZE` | *(dropped — not a meaningful access grant for reporting)* |

**POSIX mode → verbs**: `r→read`, `w→write`, `x→execute`/`list` (execute on a
directory means traversable/listable, a well-known POSIX quirk worth calling
out explicitly). **No `delete`/`change_perms`/`take_own` verb is synthesized
from a file's own mode bits** — POSIX delete permission is actually governed
by the *containing directory's* write+execute bits, not the file's own mode;
inventing a false per-entry "delete" verb here would misrepresent the model.
POSIX ACL entries (`user:`, `group:`, `mask:` tags) map the same rwx triplet
per tag; there is no allow/deny distinction (POSIX ACLs are additive-only,
§1.1) — `allow` is always `true` for a `posix_acl` `native_kind` entry.

**NFSv4/macOS-extended-ACL mask → verbs**: the NFSv4 ACE mask bit
definitions closely parallel the Windows `FILE_*` bit positions by design
(both descend from the same access-mask lineage) — `READ_DATA`,
`WRITE_DATA`, `APPEND_DATA`, `EXECUTE`, `DELETE`, `DELETE_CHILD`,
`READ_ACL`, `WRITE_ACL`, `WRITE_OWNER`, `READ_ATTRIBUTES`,
`WRITE_ATTRIBUTES` map 1:1 to the same verb table as the Windows column
above. `allow`/`deny` is a first-class ACE type field on both NFSv4 and
macOS ACEs (unlike POSIX), read directly, not inferred.

### 3.3 Where this lives centrally

**Not `items.metadata`/`user_metadata`.** Per CLAUDE.md invariant #2, those
columns hold single-current-value extracted/edited fields projected into the
disposable Meilisearch index (invariant #1). Permission data differs on three
axes that make a dedicated table the right call, not a schema violation of
the invariant:

1. **Time-series, not single-current-value** — the audit feature (§5)
   explicitly needs *multiple historical snapshots* per path to diff, which
   `items.metadata`'s "current effective value" model cannot represent
   without inventing a parallel history mechanism inside a column meant for
   something else.
2. **Not a search-projection concern** — nobody needs `allow read to
   S-1-5-21-...` as a Meilisearch-searchable facet; bloating the disposable
   index with ACE arrays serves no query use case invariant #1 exists to
   support.
3. **Not always item-scoped** — inventory roots are explicitly *not*
   required to fall inside a scanned library (the `inventory` command walks
   arbitrary agent-side paths, per `agent/internal/inventory/collector.go`'s
   own framing: "walks an expanded set of roots"). A permission snapshot
   needs to exist for paths with no corresponding `items` row at all.

Proposed additive table (naming/shape consistent with existing
`report_definitions`/`agent_replication_log`-style tables in `models.py`):

```python
class PermissionSnapshot(Base):
    __tablename__ = "permission_snapshots"

    id: UUID (uuidv7(), pk)
    agent_id: FK agents.id, ON DELETE CASCADE
    item_id: FK items.id, ON DELETE SET NULL, nullable   # best-effort link when the
                                                            # path resolves into a
                                                            # scanned library; NULL is
                                                            # the common case for a
                                                            # bare inventory root
    command_id: FK agent_commands.id, ON DELETE SET NULL, nullable  # traceability to
                                                                       # the producing run
    path: Text          # raw agent-local path, mirrors the inventory entry's "path"
    collected_at: DateTime(timezone=True), server_default now()
    owner: JSONB         # §3.1 owner shape
    aces: JSONB          # §3.1 aces array
    fidelity: Text        # full_native | synthesized_from_mode | posix_mode_only | unavailable
    raw_native: JSONB, nullable   # optional capped verbatim dump, off by default

    # Index: (agent_id, path, collected_at DESC) — latest-snapshot lookup + diffing (§5).
```

### 3.4 Reporting

Plugs into the existing canned-report machinery (`backend/filearr/reports.py`'s
`CannedReport` dataclass + `backend/filearr/api/reports.py`'s RBAC-gated
`/reports` endpoints — currently gated on `read` scope pre-RBAC, tightening to
a `download` action once Phase 6 RBAC's path-scoped ACL lands, per that
module's own documented plan). Two new report ids proposed:

- **`permissions`** — one row per (path, principal, ACE), latest snapshot per
  path, left-joined to `items` (nullable — not every permission-snapshot path
  is a catalog item), filterable by library like existing reports
  (`supports_library=True`). Well-known-principal and inherited-ACE exclusion
  (§4) applied at **query time** by default (so the underlying snapshot
  always retains the full picture for later un-filtered drill-down or
  effective-access computation — never discard data at collection time for a
  reporting-only concern).
- **`permission_changes`** — the diff report, §5.

### 3.5 Effective-access vs raw-ACL — recommend raw-ACL for v1

**Windows** has a purpose-built API for this:
[`AccessCheck`](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-accesscheck)
(walks the DACL, tallies allowed vs denied bits for a specific token, needs
generic rights pre-mapped via `MapGenericMask`) or the more modern
[`AuthzAccessCheck`](https://learn.microsoft.com/en-us/windows/win32/api/authz/nf-authz-authzaccesscheck).
Both require constructing/impersonating a token for the principal being
evaluated — non-trivial for a *different* principal than the one the agent
runs as, and Microsoft's own support content flags that effective-permission
evaluation for *remote* resources changed behavior as of Windows Server 2012
R2, an extra correctness hazard.

**POSIX** has no equivalent API at all — effective access for an arbitrary
principal requires the caller to manually evaluate mode bits + ACL entries +
**group membership** (which itself requires walking the target principal's
full group list, a separate, potentially expensive lookup) in the correct
precedence order, and doing so for macOS's ordered-ACE model differs again
from Linux's additive POSIX.1e model (§1.3).

**Recommendation**: v1 reports **raw normalized ACEs + owner only** — this is
what the schema in §3.1 already captures and is a bounded, well-defined
per-file computation with no dependency on group-membership resolution or
token construction. Effective-access-for-a-given-principal is a real, useful
future feature (documented here as a fast-follow) but pulls in materially
more platform-specific complexity and cost per file; it should not gate v1.

---

## 4. Opt-in + exclusion controls

Permissions enumeration MUST be opt-in — this brief's design keeps that at
**two independent levels**, matching the existing `InventoryConfig` pattern
in `backend/filearr/agent_config.py` (`inventory.enabled` + `inventory.
collectors[]`, itself already opt-in by construction): the collector only
runs when `"permissions"` is named in `collectors`, AND the detailed knobs
below are their own explicit block (not folded into the free-string
`collectors` list, since that list carries no configuration payload today —
mirrors how `scan_selections` is its own typed sibling field on
`GroupSettings`, not shoehorned into a string list).

```python
class PermissionsConfig(BaseModel):
    """Sibling to InventoryConfig.collectors — the detailed knobs for the
    `permissions` collector specifically. Only takes effect when `permissions`
    is ALSO named in `inventory.collectors` (defense in depth: an admin must
    both name the collector and configure it)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    include_inherited: bool = False       # default: explicit/non-inherited ACEs only
    resolve_names: bool = True            # best-effort SID/uid -> display name
    include_effective_access: bool = False  # reserved for v2; agent no-ops until shipped
    exclude_well_known: bool = True       # SYSTEM, Administrators, root, Everyone, CREATOR OWNER, ...
    exclude_principals: list[str] = []    # free canonical_id strings, same length/count
                                            # caps as InventoryConfig.collectors today
    audit: "AuditConfig | None" = None


class AuditConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    retain_snapshots: int = 10            # per-path snapshot history depth, capped
    alert_on_change: bool = False
    watch_paths: list[str] = []           # path-spec strings, same validation posture
                                            # as ScanSelection.paths (MAX_PATH_LEN, balanced brackets)
```

Wired as a new `InventoryConfig.permissions: PermissionsConfig | None = None`
field alongside the existing `collectors: list[str]`.

**Exclusion defaults** (the explicit "exclude base/system permissions" ask):
`exclude_well_known=True` and `include_inherited=False` are the defaults
**when the feature is enabled at all** — so a first-run report highlights
only explicit, non-baseline grants (the meaningful deviations an admin
actually wants to see), fully overridable per config group. Well-known
exclusion is implemented as a static table match (§1.2's well-known-SID
prefixes, plus POSIX `root`/uid 0 and a small equivalent table for common
"system" group names) — not a lookup, so it works even when name resolution
fails.

---

## 5. Auditing changes over time

**Recommended v1 mechanism: snapshot-diff**, not Windows SACL/Event Log
tailing — portable across all three OSes (SACL auditing is Windows-only and
has real setup cost, below), and it reuses the `permission_snapshots` table
(§3.3) that already exists for the raw report.

**Diff engine**: for each `(agent_id, path)`, compare the two most recent
`permission_snapshots` rows: `aces` keyed by `(principal.canonical_id,
allow)` to detect added/removed/modified entries (a modified entry = same
key, different `rights`/`inherit_flags`), plus a simple `owner` equality
check. Emit a `permission_changes` report row per detected diff — surfaced
through the same `CannedReport` machinery as §3.4's raw report.

**Alerting integration**: `AuditConfig.alert_on_change` + `watch_paths`
(config-group level) gates routing a detected diff through the **existing**
alert pipeline, mirroring `backend/filearr/alerts/ops.py`'s established
pattern for **`is_system` operational rules** (scan-failed, agent-offline
today) — a new detection hook (e.g. `alerts/permissions.py`) invoked after
each permissions-enabled inventory run, reusing
`alerts/pipeline.py`'s `compute_dedup_key` and inserting `AlertEvent` rows
through the same dedup/digest/channel-fanout machinery every other alert
already uses. **No new channel type, no new delivery mechanism** — purely a
new detection source feeding the existing sink.

**Windows SACL/Event Log (4670 "permissions changed", 4663 "object access")
as an alternative**: discussed and NOT recommended as the primary mechanism.
Per Microsoft's own documentation, both events require a SACL to already be
**explicitly configured** on the object for the relevant access type (Write
DAC/Take Ownership for 4670; the specific access type of interest for 4663)
*and* the "File System" object-access audit subcategory enabled system-wide
— a non-trivial, Windows-only, privileged (`SeSecurityPrivilege` to even set
the SACL, §1.2) setup burden per watched path, with the added cost of the
agent then needing Windows Event Log read access (typically an
`Administrators`/`Event Log Readers` group requirement). This is a real,
complementary, **near-real-time** signal worth flagging as a documented
Windows-only v3 enhancement (event-log tailing catches a change the instant
it happens; snapshot-diff is bounded by the inventory collection interval)
but should not be the v1 mechanism given its platform scope and setup cost
versus the portable, already-available snapshot-diff approach.

**Storage/retention**: `AuditConfig.retain_snapshots` (default 10) bounds
snapshot history per path — old snapshots beyond the cap are purged by the
same periodic-purge worker pattern already used for the recycle bin
(CLAUDE.md invariant #4's scheduled purge), not accumulated unboundedly. This
is a real, deliberate storage cost (every enabled watched path re-collects
and stores a full ACE list on every inventory run) and should be documented
as a capacity-planning line item alongside the existing inline/NDJSON
inventory-result size caps.

---

## 6. Honesty / hazards

- **Enumeration cost at scale**: per-file ACL/xattr syscalls (or CGO calls)
  over a slow SMB/NFS mount are the dominant cost — every OS section above
  independently confirms this is a *separate* syscall/call beyond the free
  stat, and the existing `perms`/`owner` collectors' own doc comments already
  flag this exact tradeoff for their smaller scope. The existing inventory
  framework's `DefaultMaxEntries` cap and per-command `max_entries`/
  `max_depth` bounds (`agent/internal/inventory/run.go`) already apply
  unchanged — no new rate-limiting mechanism is proposed, but operators
  should expect a `permissions`-enabled run to be markedly slower per entry
  than the existing collectors, especially over a network mount.
- **Privilege requirements are not uniform, and failure must be a reported
  health state, not silent emptiness** — SACL needs `SeSecurityPrivilege`
  (§1.2), some POSIX ACLs may be unreadable to a non-root/non-owner agent
  process, macOS ACL reads are gated by the same TCC/FDA constraint as
  content listing (§1.3). Every one of these must surface as an explicit
  per-entry or per-run diagnostic (mirrors the existing `Summary.
  CollectorErrors`/`DeniedSample` fields in `run.go`), never a
  quietly-empty `aces` array that looks like "verified no permissions
  found."
- **The mounted-share-loses-fidelity problem is the report's biggest
  credibility risk** (§2.1, §2.3) — a `permissions` collector MUST stamp
  `fidelity` per entry and the report/UI MUST surface it prominently (e.g. a
  banner on any report row sourced from a `synthesized_from_mode` or
  `posix_mode_only` fidelity), not bury it as a footnote field nobody reads.
  Reporting fabricated-looking-real ACL data is worse than reporting no ACL
  data at all.
- **Cross-host identity gaps are inherent, not a bug to fix** (§2.4) — the
  schema represents unmappable/host-scoped principals honestly; the UI/report
  must not present a `local:hostname:uid` canonical_id as if it were as
  trustworthy as a domain SID.
- **Privacy dimension**: permission data reveals organizational structure
  (who has access to what, group membership implications, org-chart-adjacent
  inference from ACL patterns) at a level plain file metadata does not. It
  rides the same agent→central trust boundary as every other inventory
  collector (mTLS-authenticated agent channel, `docs/ops/agents.md`) — no new
  transport is proposed — but this is a materially more sensitive payload
  category than size/mtime/media-type, worth flagging explicitly to whoever
  approves the RBAC-gating story for the new report (§3.4), not assumed
  equivalent to the existing canned reports' sensitivity level.

---

## 7. Task breakdown (if greenlit)

All sized S/M/L/XL, all depend on the existing `agent/internal/inventory`
Collector framework (`Registry`, `Runner`, the inline/NDJSON result channel)
being unchanged — none of this work touches that framework's contract.

| Task | Size | Description | Accept criteria | Deps |
|---|---|---|---|---|
| **W7-T1** | S | `PermissionsConfig`/`AuditConfig` pydantic models in `agent_config.py`, wired as `InventoryConfig.permissions`. | Round-trips through `validate_settings`; unknown-key rejection (`extra=forbid`) verified; length/count caps mirror `InventoryConfig.collectors`' existing posture. | none |
| **W7-T2a** | S (spike) | Resolve CGO-vs-shell-out for POSIX ACL + macOS extended-ACL reads against the agent's actual build/release pipeline (cross-compilation matrix, CI toolchain). Mirrors `phase-5-t3a-gitignore-spike.md` precedent. | Written recommendation + a throwaway proof-of-concept reading one ACL on Linux and macOS. | none |
| **W7-T2** | M | `permissions` Go collector, POSIX (Linux): full ACE list via CGO/`getfacl` per T2a's decision, `posix_acl`/`posix_mode` `native_kind` tagging, `fidelity` stamping (cifs-mount detection via `/proc/mounts`). | Unit tests against a synthetic ACL fixture; correctly flags a `cifsacl`-less mount as `synthesized_from_mode`. | T2a |
| **W7-T3** | M | `permissions` Go collector, Windows: extend the existing `GetNamedSecurityInfo`/DACL walk to the full normalized ACE list + inheritance flags + optional SACL (privilege-gated, graceful failure). Mask→verb mapping table (§3.2) as a pure, unit-tested function. | SACL read attempt surfaces a distinct health state on `ERROR_PRIVILEGE_NOT_HELD`, never an unhandled error into the walk. | none (extends existing `perms_windows.go` pattern) |
| **W7-T4** | M | `permissions` Go collector, macOS: `ACL_TYPE_EXTENDED` read + BSD `chflags` field (reuses the W6-R1-flagged `st_flags` CGO shim). | Distinguishes an ordered allow/deny ACE list from POSIX's additive model in the emitted `native_kind`. | T2a |
| **W7-T5** | S | Fidelity-tagging cross-cut: shared `fidelity` enum + cifs/NFS-version detection helpers usable by all three collectors. | A single shared package, not three duplicated heuristics. | T2, T4 |
| **W7-T6** | M | Central `permission_snapshots` table + Alembic migration + ingestion (fan permissions-collector entries from an inventory result into rows, alongside/instead of raw NDJSON storage). | A completed `permissions`-collector inventory command produces queryable snapshot rows; idempotent on redelivery (mirrors the existing inventory-results write-if-absent posture). | T2/T3/T4 (schema stable), existing `api/agent_inventory.py` |
| **W7-T7** | M | `permissions` canned report (raw ACEs + owner, exclusion filters applied at query time) wired into `reports.py`/`api/reports.py`, RBAC-gated. | Appears in `/reports` listing; respects `library_id` scoping; well-known/inherited exclusion toggles verified. | T6 |
| **W7-T8** | S | Principal-canonicalization helper module (shared Python, the §2.4 fallback chain) — used by T7 and any future consumer. | Given a raw SID/uid/name + optional domain context, returns the schema's `{kind, canonical_id, resolved}` shape; unmappable case never raises. | none (can start parallel with T1) |
| **W7-T9** | L | Snapshot diff engine + `permission_changes` report + `alerts/permissions.py` `is_system`-style detection hook wired to the existing `AlertRule`/`AlertEvent`/`compute_dedup_key` pipeline, gated by `AuditConfig`. | A synthetic before/after snapshot pair produces the expected added/removed/modified diff; `alert_on_change=false` produces zero `AlertEvent` rows. | T6, existing `alerts/pipeline.py` |
| **W7-T10** | XL (deferred, v2) | Effective-access computation (Windows `AccessCheck`/`AuthzAccessCheck`; POSIX+ACL manual evaluator with group-membership resolution). | *(not sized/sequenced now — explicitly out of scope for the v1 greenlight)* | T2–T4 |

---

## 8. Bottom-line recommendation

Build the `permissions` collector (raw normalized ACEs + owner, native
syscalls for Windows, a resolved CGO-or-shell-out approach for POSIX/macOS
ACLs per a short T2a spike) plus the normalized record schema, the additive
`permission_snapshots` table, and a single "permissions" canned report first
— opt-in via the new `inventory.permissions` config-group block, defaulting
to exclude well-known principals and inherited ACEs so a report highlights
only meaningful deviations. Fidelity tagging (§3.1, §6) must ship in the
same v1, not deferred, since a mis-fidelity-tagged report actively misleads
rather than merely under-informing. Effective-access computation (§3.5) and
change-audit/alerting (§5) are well-scoped fast-follows that build cleanly on
the same snapshot table without a schema migration — sequence them after v1
proves out ingestion volume and report usefulness in practice, not before.

## 9. Cross-verified implementation specifics (second research pass)

A second independent research pass reached the SAME v1 architecture (a strong
convergence signal); it added the following source-verified specifics, folded
in here and worth pinning before implementation:

- **POSIX ACL values decode in-house, no dependency.** v1's `perms` collector
  lists `system.posix_acl_access`/`_default` as bare xattr NAMES; reading the
  VALUES needs one `Lgetxattr` per ACL-bearing file + a ~30-line binary
  parser: 4-byte LE header (version must be `0x0002`) then N 8-byte
  `{tag u16, perm u16, id u32}` entries (tags ACL_USER_OBJ 0x01 … ACL_OTHER
  0x20; `id` = uid/gid only for ACL_USER/ACL_GROUP, else `0xFFFFFFFF`). Define
  the constants in-house (the `agent-inventory-presets.md` §1.2 precedent) —
  the third-party Go ACL libs either wrap libc (reintroduces the CGO/exec
  question) or are thinly maintained. `system.posix_acl_default` uses the
  identical format, directory-only, and must be captured as a SEPARATE
  `acl_default`/`scope: dir_default` list (who children INHERIT ≠ who can
  access the dir today).
- **macOS ACLs are CGO-blocked — accept the `ls -le` exec fallback.** macOS
  has NFSv4-style ACLs via `acl_get_file`, a libSystem call requiring cgo; the
  agent fleet builds `CGO_ENABLED=0` (verified in `.github/workflows/ci.yml`,
  same constraint the thumbnailer already documents). v1 therefore reads macOS
  ACLs by exec'ing `ls -le` PER DIRECTORY (batches process-spawn cost) with
  `LC_ALL=C` forced (locale-independent trustee rendering). This is the most
  fragile collector (text parse vs typed API) — an accepted, documented
  trade-off, not a surprise.
- **Linux NFSv4 ACLs are mount-gated, never local.** `richacl` was never
  merged into the mainline VFS, so a local ext4/xfs/btrfs file has no NFSv4
  ACL concept — `system.nfs4_acl`/`nfs4_getfacl` are only meaningful when the
  walked root IS an `nfs4` client mount. Detect fstype first (`/proc/mounts`
  or `statfs.f_type`); elsewhere the NFSv4 branch is a documented no-op. Read
  via `nfs4_getfacl` (nfs4-acl-tools) text output, not a hand-rolled wire
  decoder, for that narrow gated case.
- **Go-native SMB security-descriptor reads need a specific fork.** A Windows
  agent reads UNC-path ACLs transparently via `GetNamedSecurityInfo` (no extra
  code). A Linux agent NOT on the file server: the canonical
  `hirochachacha/go-smb2` has NO security-descriptor query (verified in its
  `client.go`); the `medianexapp/go-smb2` fork adds `File.SecurityInfo()` /
  `Share.SecurityInfo()` — the concrete starting point IF Go-native SMB ACL
  reads are ever needed, pending a license/maintenance check. Otherwise
  `smbcacls` (Samba client CLI) queries the raw remote SD without the lossy
  `cifsacl` mount option. NFS has no share-ACL analog; flag the NFSv3
  numeric-uid-coincidence, NFSv4 idmapd squash-to-`nobody`, and `root_squash`
  effective-access pitfalls in the reconciliation layer.
- **Native change-auditing is honestly per-OS-gated; snapshot-diff is the only
  portable default.** Windows events 4670 (DACL change) / 4907 (SACL change)
  need BOTH the audit subcategory enabled AND a matching SACL present, plus
  `SeSecurityPrivilege`/Event-Log-Readers to read — scope strictly to those
  two IDs (broad file-system audit is high-volume). Linux `auditd -w` watches
  are inode-pinned (rename silently detaches); `fanotify FAN_MARK_FILESYSTEM`
  needs `CAP_SYS_ADMIN` and can overflow its 16 K queue. macOS Endpoint
  Security needs an Apple-granted `com.apple.developer.endpoint-security.client`
  entitlement — a business/process approval, NOT an engineering task: state it
  as **"not pursued"**, never "deferred," so it can't silently resurface as a
  blocker.
- **Normalized-record refinements**: keep an `order_index` per ACE (raw
  storage order, NEVER re-sorted — Windows DACL evaluation is order-dependent)
  and a `posture` block (`dacl_present`, `dacl_canonical`,
  `generic_mapping_applied`); map POSIX mode bits onto the SAME `entries`
  shape as three synthetic `posix_user_obj`/`group_obj`/`other` entries so a
  report queries "who has read" uniformly across DACL / POSIX mode / ACL.

### 9.1 Open questions for the architect (before greenlight)

1. **Storage shape**: one wide JSONB blob per (item, run) — simplest, matches
   the `user_metadata` precedent — vs a normalized one-row-per-ACE child table
   needed to index `access_by_principal`/`broad_access` at scale. Leaning
   child table; migration-cost tradeoff unresolved.
2. **Central-scanner parity**: `perms`/`owner` is agent-only today; a
   centrally-scanned (agentless) library has no permission capture at all,
   though POSIX mode bits are free from the scanner's existing stat. In scope
   to grow a minimal central capture on the same schema, or separate?
3. **Exclusion escape hatch**: confirm report-side-default (collection stays
   full-fidelity) vs also shipping the narrow collection-side toggle now.
4. **SACL privilege model**: the Windows service runs LocalSystem (kardianos
   default) — accept in-process `AdjustTokenPrivileges` for SACL, or require a
   distinct provisioned service account?
5. **Samba share-ACL**: no remote-queryable API (only reading `smb.conf` on
   the host) — Linux-agent-on-Samba-host special case, or documentation-only?
6. **Drift-retention default**: mirror the alerting 30-day precedent, or treat
   permission-drift retention as a compliance-driven product input?

---

## Scaffold status (W7)

Central-side (Python) scaffolding landed per the project's scaffolding
convention: the PURE cores are implemented for real and unit-tested; every
stateful/DB/report-query boundary is an inert typed stub or documented-only DDL;
no migration; ruff + suite stay green (`alembic heads` unchanged at
`c7d9e1f3a5b8`).

### Scaffolded now (real + tested)

- **`backend/filearr/permissions.py`** — the normalized record schema (§3.1/§3.2/
  §9): `Principal`, `Ace`, `PermissionRecord`, `Posture`, plus the `Verb` /
  `PrincipalKind` / `AceType` / `AceScope` / `AceSource` / `NativeKind` /
  `Fidelity` vocabularies (Pydantic, `Principal`/`Ace`/`Posture` frozen,
  `extra=forbid`; JSON round-trip verified).
- **Well-known table + `filter_entries(...)`** — the pure core of the "exclude
  base/system permissions" knob (§4): a static SID/uid/name table
  (`WELL_KNOWN_SIDS`, the `S-1-5-32-*` BUILTIN prefix, POSIX uid 0 + system
  names) and `is_well_known()`, with the exclusion filter defaulting to
  `exclude_well_known=True` / `include_inherited=False`. Owner/group are never
  filtered; filtering is pure (input untouched) and reporting-view only.
- **`diff_records(...)`** — the snapshot-diff engine (§5.1): added/removed/
  modified ACEs keyed on `(principal.canonical_id, type, scope)` + owner/group
  change, order-independent on verbs/flags; handles first-snapshot, deletion,
  both-None, mask/rights change, and allow-vs-deny-distinct-key.
- **`PermissionsConfig` / `AuditConfig`** in `backend/filearr/agent_config.py`,
  wired as `InventoryConfig.permissions` (§4) — additive, opt-in, defaults OFF,
  validated (bounds/caps, `extra=forbid`). Omitting the block leaves an existing
  `GroupSettings` valid (backward compatible). An admin can author permissions
  config ahead of the collector.
- Tests: `backend/tests/test_permissions_w7.py`.

### Inert in this scaffold (documented, not wired)

- **All native OS reads** happen AGENT-side (Go): SID/DACL walk, POSIX ACL/xattr
  decode, macOS `ls -le`, cifs-mount fidelity detection, native-mask→verb
  mapping. Central stores the already-normalized record and preserves the raw
  mask verbatim.
- **`permission_snapshots` storage** — intended DDL is documented only
  (`INTENDED_PERMISSION_SNAPSHOTS_DDL` in `permissions.py`); NOT a live model,
  NOT on `Base.metadata`, no migration. §9.1 wide-JSONB-vs-child-table question
  is still open (see below).
- **Canned reports** — `permissions_report_access_by_principal`, `_broad_access`,
  `_explicit_ace_outliers`, `_permission_drift` are typed builders raising
  `NotImplementedError("permissions report: scaffold, W7-Tn")`; NOT in the live
  `filearr.reports._REPORTS` registry (the registration seam is documented in
  `permissions.py`). `GET /api/v1/reports` still lists the original six.
- **Snapshot persistence / ingestion / alert routing** — none present (agent-side
  collector + W7-T6 ingestion not built).

### Ordered next steps (brief T-numbers)

1. **W7-T2a** (spike) — CGO-vs-shell-out for POSIX/macOS ACL reads.
2. **W7-T2/T3/T4** — the Go `permissions` collector per OS (emits this schema).
3. **W7-T6** — promote `INTENDED_PERMISSION_SNAPSHOTS_DDL` to a live model + a
   real Alembic migration + inventory-result ingestion (fan entries into rows).
4. **W7-T7** — register the `permissions` canned report(s), applying
   `filter_entries` at query time; RBAC-gate per `api/reports.py`.
5. **W7-T8** — the shared principal-canonicalization helper (§2.4 fallback chain).
6. **W7-T9** — wire `diff_records` to the `permission_changes` report +
   `alerts/permissions.py` detection hook, gated by `AuditConfig`.

### 6 open questions still needing an architect ruling (§9.1)

1. **Storage shape** — wide JSONB per (path, run) vs one-row-per-ACE child table
   (indexing `access_by_principal`/`broad_access` at scale). Gates W7-T6; the
   record schema supports either.
2. **Central-scanner parity** — grow a minimal agentless POSIX-mode capture on
   the same schema, or keep permissions agent-only?
3. **Exclusion escape hatch** — report-side default only (collection stays
   full-fidelity), or also ship the collection-side toggle now? (The
   `PermissionsConfig` knobs are authored but consumed agent-side.)
4. **SACL privilege model** — accept in-process `AdjustTokenPrivileges` on the
   LocalSystem service, or require a distinct provisioned account?
5. **Samba share-ACL** — no remote-queryable API; Linux-agent-on-Samba-host
   special case, or documentation-only?
6. **Drift-retention default** — mirror the alerting 30-day precedent, or treat
   as a compliance-driven product input? (`AuditConfig.retain_snapshots`
   defaults to 10 per-path snapshots for now.)

All examples above use `example.com`-class placeholders only.
