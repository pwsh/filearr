"""W7 — permissions enumeration/reconciliation/audit: central-side PURE cores.

Scaffolding for the ``permissions`` collector feature researched in
``docs/research/permissions-enumeration-audit.md``. This module holds the
request-free, DB-free, unit-testable cores that the central side owns:

* the normalized permission-record schema (§3.1/§3.2/§9) — :class:`Principal`,
  :class:`Ace`, :class:`PermissionRecord`, plus the :class:`Verb` /
  :class:`Fidelity` / :class:`AceType` / :class:`AceScope` / :class:`AceSource`
  vocabularies;
* the well-known-principal table + :func:`filter_entries` — the pure core behind
  the operator's "exclude base/system permissions" knob (§4);
* :func:`diff_records` — the snapshot-diff engine (§5.1), pure and DB-free.

Deliberately INERT in this scaffold (documented, not wired):

* **Native OS reads** (SID/DACL walk, POSIX ACL/xattr decode, macOS ``ls -le``,
  cifs-mount fidelity detection) live AGENT-side (Go) — see the brief §1/§2.
  Central never touches an OS permission API; it stores the normalized record the
  agent emits. The native-mask → :class:`Verb` mapping tables (§3.2) are the
  AGENT's responsibility; central validates/stores the already-normalized verbs
  and preserves the raw mask verbatim (:attr:`Ace.raw_mask`).
* **Storage** — the intended ``permission_snapshots`` table is DOCUMENTED ONLY
  (:data:`INTENDED_PERMISSION_SNAPSHOTS_DDL`); it is NOT a live SQLAlchemy model
  and is NOT registered on ``Base.metadata`` (no migration this phase).
* **Reports** — the four permission-report builders are typed stubs raising
  ``NotImplementedError``; they are NOT in the live canned-report registry.

§9.1 open storage question (unresolved, for the architect): whether a snapshot
persists as ONE wide JSONB blob per (path, run) — simplest, mirrors the
``user_metadata`` precedent — or as a normalized one-row-per-ACE child table
needed to index ``access_by_principal`` / ``broad_access`` at scale. The record
schema below is storage-shape-agnostic: it round-trips to a single JSON object
(wide-blob friendly) yet exposes ``entries`` as a flat list a child-table writer
can fan out one row per ACE. The ruling gates W7-T6.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select

from filearr.reports import ReportParams

# --------------------------------------------------------------------------- #
# Vocabularies                                                                 #
# --------------------------------------------------------------------------- #


class Verb(str, enum.Enum):
    """The normalized cross-OS verb set (§3.2).

    The native-mask → verb mapping (NTFS ``FILE_*`` bits, POSIX ``rwx``,
    NFSv4/macOS ACE mask bits) happens AGENT-side — the Go collector owns those
    per-OS tables. Central stores what it is given: the already-normalized verb
    list PLUS the verbatim :attr:`Ace.raw_mask` for forensic drill-down. This
    enum is the shared contract the agent normalizes TO."""

    read = "read"
    write = "write"
    execute = "execute"
    append = "append"
    delete = "delete"
    delete_child = "delete_child"
    list = "list"
    read_attr = "read_attr"
    write_attr = "write_attr"
    read_perms = "read_perms"
    change_perms = "change_perms"
    take_ownership = "take_ownership"
    full = "full"


class PrincipalKind(str, enum.Enum):
    """How much the agent could resolve a principal (§2.4 fallback chain)."""

    user = "user"
    group = "group"
    well_known = "well_known"  # a static-table SID/uid (SYSTEM, Everyone, root, ...)
    unmapped = "unmapped"  # an orphaned SID / uid with no account (never dropped)


class AceType(str, enum.Enum):
    """allow | deny. Windows/NFSv4/macOS carry both; POSIX ACLs are allow-only."""

    allow = "allow"
    deny = "deny"


class AceScope(str, enum.Enum):
    """Where an ACE applies, derived from inherit flags / POSIX default-vs-access."""

    this = "this"  # this object only
    subtree = "subtree"  # propagates to children (container/object inherit)
    dir_default = "dir_default"  # POSIX default ACL: who CHILDREN inherit, not access now


class AceSource(str, enum.Enum):
    """Which permission layer an ACE came from (§2.1). Never blended into one
    verdict in v1 — effective-access is the intersection and is deferred (§3.5)."""

    local = "local"  # filesystem ACL on the object itself
    share = "share"  # share-level ACL (SMB share security descriptor)


class NativeKind(str, enum.Enum):
    """The native permission model an entry was normalized from (§3.1)."""

    ntfs = "ntfs"
    posix_acl = "posix_acl"
    posix_mode = "posix_mode"
    nfsv4 = "nfsv4"
    macos_acl = "macos_acl"


class Fidelity(str, enum.Enum):
    """How trustworthy the read is (§2.3/§6). A ``synthesized_from_mode`` or
    ``posix_mode_only`` record is NOT the server's real ACL and MUST be surfaced
    prominently, never buried — reporting fabricated-looking ACL data is worse
    than reporting none."""

    full_native = "full_native"
    synthesized_from_mode = "synthesized_from_mode"
    posix_mode_only = "posix_mode_only"
    unavailable = "unavailable"


# --------------------------------------------------------------------------- #
# Normalized record schema (§3.1 / §9)                                          #
# --------------------------------------------------------------------------- #


class Principal(BaseModel):
    """One security principal (owner, group, or an ACE trustee), §3.1/§2.4.

    ``canonical_id`` is the strongest-available cross-host key (a domain AD SID
    is portable; a local SID / bare uid is host-scoped and the agent qualifies it
    as ``local:<host>:<id>``); ``source_identifier`` preserves the RAW id
    verbatim always. An unmappable principal is kept with ``resolved=False`` and
    ``kind=unmapped`` — never dropped (§2.4.5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PrincipalKind
    canonical_id: str
    source_identifier: str
    display: str | None = None
    domain: str | None = None
    resolved: bool = False


class Ace(BaseModel):
    """One access-control entry, normalized (§3.1/§3.2/§9).

    ``verbs`` is the normalized set; ``raw_mask`` preserves the native mask
    verbatim (hex for NTFS/NFSv4, octal+tag for POSIX). ``order_index`` preserves
    raw storage order (Windows DACL evaluation is order-dependent — NEVER
    re-sorted). ``inherited`` distinguishes an ACE that flowed down from a parent
    from one explicitly set here; ``inherit_flags`` are the raw propagation flags
    (container_inherit / object_inherit / inherit_only / no_propagate)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal: Principal
    type: AceType
    verbs: tuple[Verb, ...] = ()
    raw_mask: str = ""
    native_kind: NativeKind | None = None
    inherited: bool = False
    inherit_flags: tuple[str, ...] = ()
    scope: AceScope = AceScope.this
    source: AceSource = AceSource.local
    order_index: int = 0


class Posture(BaseModel):
    """Per-record security-descriptor posture (§9). Windows-centric flags that a
    report needs to caveat a row (a non-canonical DACL, generic-mask expansion
    applied, or an entirely absent DACL — which is "everyone full", not "no
    access")."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dacl_present: bool = True
    dacl_canonical: bool = True
    generic_mapping_applied: bool = False


class PermissionRecord(BaseModel):
    """The full normalized permission record for one path (§3.1).

    ``owner``/``group`` are separate principals (not ACEs). ``entries`` is the
    flat, order-preserved ACE list. ``fidelity`` stamps how trustworthy the read
    is. ``raw_native`` is an optional capped verbatim dump (SDDL / getfacl /
    nfs4_getfacl text), off by default."""

    model_config = ConfigDict(extra="forbid")

    owner: Principal | None = None
    group: Principal | None = None
    entries: tuple[Ace, ...] = ()
    fidelity: Fidelity = Fidelity.full_native
    posture: Posture | None = None
    raw_native: str | None = None


# --------------------------------------------------------------------------- #
# Well-known principal table + exclusion filter (§1.2 / §4)                     #
# --------------------------------------------------------------------------- #
#: Well-known Windows SIDs (§1.2, Microsoft's fixed OS-version-independent table).
#: Recognized by a static string match, NOT a lookup, so exclusion works even
#: when name resolution fails. This is the headline "exclude base permissions"
#: knob's backing table.
WELL_KNOWN_SIDS: frozenset[str] = frozenset(
    {
        "S-1-0-0",  # Nobody
        "S-1-1-0",  # Everyone
        "S-1-3-0",  # CREATOR OWNER
        "S-1-3-1",  # CREATOR GROUP
        "S-1-3-2",  # CREATOR OWNER SERVER
        "S-1-3-3",  # CREATOR GROUP SERVER
        "S-1-5-7",  # ANONYMOUS LOGON
        "S-1-5-11",  # Authenticated Users
        "S-1-5-13",  # Terminal Server Users
        "S-1-5-18",  # SYSTEM / NT AUTHORITY\SYSTEM (LocalSystem)
        "S-1-5-19",  # LOCAL SERVICE
        "S-1-5-20",  # NETWORK SERVICE
    }
)

#: Any SID under the BUILTIN domain (``S-1-5-32-*``: Administrators 544, Users
#: 545, Guests 546, Power Users 547, and the rest) is well-known by prefix.
BUILTIN_SID_PREFIX = "S-1-5-32-"

#: Well-known POSIX numeric ids: uid/gid 0 = root/system, whatever encoding the
#: agent used (bare ``0``, ``uid:0``, ``local:<host>:0``, ``posix:0`` — matched
#: on the trailing id segment).
WELL_KNOWN_POSIX_IDS: frozenset[str] = frozenset({"0"})

#: A small equivalent table of common POSIX "system" account/group names (§4),
#: matched when an id resolves by name only.
WELL_KNOWN_POSIX_NAMES: frozenset[str] = frozenset(
    {"root", "wheel", "daemon", "bin", "sys", "adm", "nogroup", "nobody"}
)


def _is_well_known_id(ident: str | None) -> bool:
    """Static-table membership test for a single raw identifier string."""
    if not ident:
        return False
    s = ident.strip()
    up = s.upper()
    if up in WELL_KNOWN_SIDS or up.startswith(BUILTIN_SID_PREFIX):
        return True
    # POSIX id, possibly host-qualified (``local:host:0``) or tag-wrapped
    # (``uid:0``): test the trailing segment.
    tail = s.rsplit(":", 1)[-1]
    if tail in WELL_KNOWN_POSIX_IDS:
        return True
    return tail.lower() in WELL_KNOWN_POSIX_NAMES


def is_well_known(principal: Principal) -> bool:
    """True when a principal is a base/system principal to hide by default (§4).

    Belt and braces: honors an agent classification of ``kind=well_known`` AND
    independently matches the static SID/uid/name table over both the canonical
    and raw identifiers — so the exclusion holds even if the agent did not (or
    could not) pre-classify the principal."""
    if principal.kind is PrincipalKind.well_known:
        return True
    return _is_well_known_id(principal.canonical_id) or _is_well_known_id(
        principal.source_identifier
    )


def _principal_matches(principal: Principal, ids: set[str]) -> bool:
    """True when a principal's canonical or raw identifier is in an exclude set."""
    return principal.canonical_id in ids or principal.source_identifier in ids


def filter_entries(
    record: PermissionRecord,
    *,
    exclude_well_known: bool = True,
    include_inherited: bool = False,
    exclude_principals: list[str] | None = None,
) -> PermissionRecord:
    """Return a copy of ``record`` with ACEs filtered per the exclusion knobs (§4).

    This is the pure core of the feature's headline "exclude base/system
    permissions" behavior. Defaults match the collector's opt-in defaults so a
    first-run report highlights only explicit, non-baseline grants:

    * ``exclude_well_known=True`` — drop SYSTEM / Administrators / Everyone /
      CREATOR OWNER / root / ... (:func:`is_well_known`).
    * ``include_inherited=False`` — drop ACEs that flowed down from a parent,
      keeping only explicit grants set on this object.
    * ``exclude_principals`` — drop any ACE whose principal's canonical or raw id
      is explicitly listed.

    The ``owner``/``group`` fields are NEVER filtered (they are not ACEs and a
    report always shows who owns the object). Filtering is a reporting-view
    concern only — collection retains the full record (§3.4), so this is applied
    at query time, never at collection time."""
    ids = {e.strip() for e in (exclude_principals or []) if e and e.strip()}
    kept: list[Ace] = []
    for ace in record.entries:
        if exclude_well_known and is_well_known(ace.principal):
            continue
        if not include_inherited and ace.inherited:
            continue
        if ids and _principal_matches(ace.principal, ids):
            continue
        kept.append(ace)
    return record.model_copy(update={"entries": tuple(kept)})


# --------------------------------------------------------------------------- #
# Snapshot-diff engine (§5.1)                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AceModification:
    """One same-key ACE whose material grant changed between two snapshots."""

    before: Ace
    after: Ace


@dataclass(frozen=True)
class PermissionDiff:
    """The computed difference between two permission snapshots (§5.1).

    ACEs are keyed on ``(principal.canonical_id, type, scope)``; a same-key ACE
    with a different material grant (verbs / inherit flags / raw mask /
    native_kind / source / inherited) is a *modification*, not a
    remove-plus-add. ``owner_changed`` is a simple owner-canonical-id inequality
    (a display/domain-only change is NOT an owner change)."""

    added: tuple[Ace, ...] = ()
    removed: tuple[Ace, ...] = ()
    modified: tuple[AceModification, ...] = ()
    owner_changed: bool = False
    owner_before: Principal | None = None
    owner_after: Principal | None = None
    group_changed: bool = False
    group_before: Principal | None = None
    group_after: Principal | None = None

    @property
    def is_empty(self) -> bool:
        """True when nothing changed (no ACE add/remove/modify, no owner/group)."""
        return not (
            self.added
            or self.removed
            or self.modified
            or self.owner_changed
            or self.group_changed
        )


def _ace_key(a: Ace) -> tuple[str, str, str]:
    return (a.principal.canonical_id, a.type.value, a.scope.value)


def _ace_material(a: Ace) -> tuple:
    """The comparable grant of an ACE (order-independent for verbs/flags)."""
    return (
        frozenset(a.verbs),
        frozenset(a.inherit_flags),
        a.raw_mask,
        a.native_kind,
        a.source,
        a.inherited,
    )


def _principal_key(p: Principal | None) -> str | None:
    return None if p is None else p.canonical_id


def diff_records(
    old: PermissionRecord | None, new: PermissionRecord | None
) -> PermissionDiff:
    """Compute the ACE/owner/group diff between two snapshots (§5.1), pure.

    Cases handled:

    * ``old=None`` (first snapshot) — every ``new`` ACE is *added*; owner/group
      count as changed iff now set.
    * ``new=None`` (the path's permissions vanished / deletion) — every ``old``
      ACE is *removed*; owner/group changed iff they were set.
    * both ``None`` — an empty diff.
    * both present — set difference on the ACE key, with same-key material
      changes reported as modifications.

    Never raises; a malformed pair simply yields the best-effort diff."""
    old_entries = old.entries if old is not None else ()
    new_entries = new.entries if new is not None else ()

    old_by_key: dict[tuple[str, str, str], Ace] = {_ace_key(a): a for a in old_entries}
    new_by_key: dict[tuple[str, str, str], Ace] = {_ace_key(a): a for a in new_entries}

    added = tuple(a for k, a in new_by_key.items() if k not in old_by_key)
    removed = tuple(a for k, a in old_by_key.items() if k not in new_by_key)
    modified = tuple(
        AceModification(before=old_by_key[k], after=new_by_key[k])
        for k in new_by_key.keys() & old_by_key.keys()
        if _ace_material(old_by_key[k]) != _ace_material(new_by_key[k])
    )

    old_owner = old.owner if old is not None else None
    new_owner = new.owner if new is not None else None
    old_group = old.group if old is not None else None
    new_group = new.group if new is not None else None

    return PermissionDiff(
        added=added,
        removed=removed,
        modified=modified,
        owner_changed=_principal_key(old_owner) != _principal_key(new_owner),
        owner_before=old_owner,
        owner_after=new_owner,
        group_changed=_principal_key(old_group) != _principal_key(new_group),
        group_before=old_group,
        group_after=new_group,
    )


# --------------------------------------------------------------------------- #
# DOCUMENTED-ONLY intended DDL (§3.3) — NOT a live model, NOT on Base.metadata  #
# --------------------------------------------------------------------------- #
# The ``permission_snapshots`` table is intentionally NOT declared as a live
# SQLAlchemy model in this scaffold: declaring it would register it on
# ``Base.metadata`` and pull it into ``create_all`` / autogenerate. Per the
# project's scaffolding convention the intended shape is documented HERE as an
# inert source string (never executed, never imported by the ORM) so W7-T6 can
# promote it into ``models.py`` + a real Alembic migration when storage lands.
#
# Open storage question (§9.1) the ruling must settle before promotion: this wide
# ``owner``/``aces``/``raw_native`` JSONB shape (one row per (path, run)) vs a
# normalized one-row-per-ACE child table needed to index access_by_principal /
# broad_access at scale. The record schema above supports either.
INTENDED_PERMISSION_SNAPSHOTS_DDL = '''
class PermissionSnapshot(Base):
    """One collected permission snapshot for one agent path (§3.3). Additive,
    time-series (multiple historical rows per path to diff, §5) — deliberately
    NOT items.metadata (invariant 2: single-current-value, search-projected)."""

    __tablename__ = "permission_snapshots"
    __table_args__ = (
        # Latest-snapshot lookup + diff pairing (§5): newest row per (agent, path).
        Index(
            "ix_permission_snapshots_agent_path_time",
            "agent_id",
            "path",
            text("collected_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE")
    )
    # Best-effort link when the path resolves into a scanned library; NULL is the
    # COMMON case — a bare inventory root need not be a catalog item (§3.3.3).
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id", ondelete="SET NULL"), nullable=True
    )
    # Traceability to the producing inventory run (SET NULL so history outlives it).
    command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_commands.id", ondelete="SET NULL"),
        nullable=True,
    )
    path: Mapped[str] = mapped_column(Text)            # raw agent-local path
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    owner: Mapped[dict | None] = mapped_column(JSONB, nullable=True)   # §3.1 owner
    aces: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    fidelity: Mapped[str] = mapped_column(Text)        # Fidelity value
    # Content digest over the normalized record: diff-gating so an unchanged
    # re-collection can skip writing a new row (§5 retain_snapshots economy).
    digest: Mapped[str] = mapped_column(Text)
    raw_native: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # opt-in
'''


# --------------------------------------------------------------------------- #
# REPORT BUILDER STUBS (§3.4 / §5) — typed, INERT, NOT in the live registry      #
# --------------------------------------------------------------------------- #
# Registration seam: to go live, each builder below is paired with a row
# serializer into a ``filearr.reports.CannedReport(id=..., build=..., row=...,
# supports_library=True)`` and appended to ``filearr.reports._REPORTS`` (so it
# appears in ``list_reports()`` / ``GET /api/v1/reports`` and is RBAC-gated by the
# existing ``api/reports.py`` machinery). They are intentionally NOT registered in
# this scaffold — raising here keeps them off the runnable surface (W7-T7/T9).
#
# Each takes the existing ``ReportParams`` (library_id + limit) and returns a
# single streamable SQLAlchemy ``Select`` over the future ``permission_snapshots``
# table, so it slots into ``stream_report_rows`` / ``render_rows`` unchanged.


def permissions_report_access_by_principal(params: ReportParams) -> Select:
    """Raw ACEs + owner, latest snapshot per path (§3.4 ``permissions`` report),
    exclusion filters (§4) applied at query time. NOT registered (scaffold)."""
    raise NotImplementedError("permissions report: scaffold, W7-T7")


def _broad_access(params: ReportParams) -> Select:
    """Paths granting a broad principal (Everyone / Authenticated Users) an
    explicit non-inherited grant (§9.1 broad_access). NOT registered (scaffold)."""
    raise NotImplementedError("permissions report: scaffold, W7-T7")


def _explicit_ace_outliers(params: ReportParams) -> Select:
    """Explicit (non-inherited) ACEs that deviate from a path's inherited
    baseline — the meaningful-deviation view (§4). NOT registered (scaffold)."""
    raise NotImplementedError("permissions report: scaffold, W7-T7")


def _permission_drift(params: ReportParams) -> Select:
    """The ``permission_changes`` diff report over consecutive snapshots (§5).
    NOT registered (scaffold)."""
    raise NotImplementedError("permissions report: scaffold, W7-T9")
