"""Agent file-transfer + share-location core (Phase 10, roadmap item 10 /
``docs/research/phase-10-agent-file-transfer.md`` + user-mandated share-location
model, ``docs/tasks/phase-10-agent-transfer-tasks.md``).

**Inert scaffolding.** Nothing in the runtime imports this module yet — only its
tests (and the deliberately-501 API router in :mod:`filearr.api.transfers`, whose
*contract* ships early so clients can code against it) do. It carries the
*pure*, unit-testable core of the agent→central retrieval + share-mapping
contract plus typed ``NotImplementedError`` stubs for the stateful pieces, each
tagged with the Phase-10 task (``P10-Tk``) that will implement it. Wiring the
staging/tus data plane + SSE is P10-T4/T6/T13 — see the tasks doc.

What is *pure and implemented here*:

- :class:`AgentCommand` / :class:`CommandResult` — the on-the-wire shape of the
  new ``agent_commands`` primitive (brief §3.1): the one poll-delivered
  instruction covering ``stat_check`` / ``rehash_check`` / ``stage_upload``.
- :class:`TransferRequest` / :class:`TransferStatus` — the retrieve-endpoint
  request + status contracts (brief §5, §6), real enough that the API router's
  input validation (422) is meaningful today.
- :class:`ShareHint` — the additive ``share_hint`` an agent may attach to a
  replicated item (P10-T11), so central can render a "network-open" link for an
  agent-hosted file exactly like ``library.share_prefix`` does for centrally
  scanned ones (:mod:`frontend/src/lib/pathlinks`).
- :class:`ShareMapping` — the central fallback (P10-T12) when an agent can't
  self-report: an admin-defined ``(agent_id|library, local_prefix) →
  share_prefix`` rule.
- :func:`classify_prefix` — server-side mirror of the frontend ``classifyPrefix``
  (UNC / URL-scheme / posix), so hint + mapping schemes agree end to end.
- :func:`resolve_share_url` — longest-``local_prefix``-wins resolution of a
  local file path to a share URL, reusing the ``resolve_scan_path`` /
  ``pathlinks`` longest-prefix discipline (case-preserving, separator-safe).
- :func:`transfer_state_machine` — the retrieve lifecycle transition function
  (``pending → uploading → staged → downloaded``; ``expired`` / ``failed``
  terminals). Pure; an invalid ``(state, event)`` raises ``ValueError``.
- :func:`staging_path_for` — content-addressed staging path for a transfer id.
  Traversal-proof **by construction**: the id is parsed as a UUID, so the
  resulting filename can only ever be ``[0-9a-f-]`` — no ``..``, no separator,
  no attacker-controlled component can escape :data:`STAGING_ROOT`.

Architect-style rulings baked in (see the tasks doc for the full list):

- **R1** — share discovery is **best-effort**: anonymous-share visibility,
  per-OS permission quirks, and multi-homed hosts mean a missing/blank
  :class:`ShareHint` is normal, not an error — the central mapping (P10-T12) is
  the deterministic fallback, never overridden by a hint that later disagrees.
- **R2** — RBAC ``download`` gates ``agent_commands`` **creation** (before any
  agent bandwidth / central disk is spent), and the completed retrieve audits
  **unconditionally** regardless of ``FILEARR_AUDIT_READS`` (brief §4). Enforced
  in the API layer (P10-T13 / Wave 4), documented on every stub here.
- **R3** — the item→agent routing column the retrieve path keys on already
  exists as ``items.source_agent_id`` (added by the Phase-5 scaffold); P10-T2's
  ``items.agent_id`` reconciles to it rather than adding a second authority.

No ``models.py`` change, no migration, no runtime wiring lands in this pass. The
intended DDL (``agent_commands``, ``staging_transfers``, ``agent_share_maps``)
is spelled out in the tasks doc so the implementing task writes the revision.
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- agent_commands primitive (brief §3.1) ---------------------------------

CommandKind = Literal["stat_check", "rehash_check", "stage_upload"]
CommandStatus = Literal["pending", "picked_up", "done", "failed", "expired"]


class AgentCommand(BaseModel):
    """One row of the new ``agent_commands`` table (brief §3.1), delivered on the
    same authenticated poll channel Phase-5 already built (P5-T2/T6).

    ``kind`` keeps existence-check (``stat_check`` / ``rehash_check``) and
    retrieve-trigger (``stage_upload``) on a single primitive, per the brief's
    osquery ``distributed_interval`` precedent. ``requested_by`` is the audit
    actor (principal id) — carried so the completed retrieve can be attributed
    (brief §4.5). Idempotent by construction: re-picking-up a ``done`` row is a
    no-op (same posture as replication's ``(agent_id, seq_no)`` upsert)."""

    model_config = ConfigDict(frozen=True)

    id: str
    agent_id: str
    kind: CommandKind
    item_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: CommandStatus = "pending"
    requested_by: str | None = None


class CommandResult(BaseModel):
    """The ``result`` an agent reports back for a picked-up :class:`AgentCommand`
    (brief §3.1 ``result`` JSONB / §3.2). For ``stat_check`` only
    ``exists`` / ``size`` / ``mtime`` are set; ``rehash_check`` additionally
    fills ``quick_hash`` (and ``content_hash`` only when explicitly requested).
    ``exists=False`` drives the invariant-4 ``missing`` tombstone path.

    ``content_skipped`` is the additive P10-T3 field (validated on the central
    side as part of this contract, per the tasks doc): the agent sets it True on
    a ``rehash_check`` when the ``content`` hash was requested but NOT computed —
    the file is absent (``exists=False``) or above the agent's local
    ``FullMaxBytes`` ceiling — so central reads ``content_hash=None`` as "not
    computed", never "the file has no content hash". It stays ``False`` for a
    ``stat_check`` (content is never requested there)."""

    model_config = ConfigDict(frozen=True)

    exists: bool
    size: int | None = None
    mtime: float | None = None
    quick_hash: str | None = None
    content_hash: str | None = None
    # Additive P10-T3 field: True when a requested content hash was skipped
    # (missing file or oversize) so ``content_hash=None`` is unambiguous.
    content_skipped: bool = False


# --- retrieve endpoint request/status contracts (brief §5, §6) -------------


class TransferRequest(BaseModel):
    """Body of ``POST /api/v1/items/{id}/transfer`` (P10-T13).

    Deliberately minimal but *real*, so the router's 422 validation is
    meaningful today. ``verify_hash`` requests the streaming integrity check on
    staging completion (brief §2.3; default on — integrity over speed).
    ``max_bytes_per_sec`` is an optional per-transfer rate override folded into
    the agent's policy-delivered token bucket (brief §2.4); it must be a
    positive byte rate when set (``gt=0`` → 422 on a non-positive value)."""

    model_config = ConfigDict(extra="forbid")

    verify_hash: bool = True
    max_bytes_per_sec: int | None = Field(default=None, gt=0)


TransferState = Literal[
    "pending", "uploading", "staged", "downloaded", "expired", "failed"
]


class TransferStatus(BaseModel):
    """Response of ``GET /api/v1/transfers/{id}`` (P10-T13) and the payload the
    SSE progress stream mirrors (brief §5 states). ``bytes_transferred`` /
    ``total_bytes`` drive the progress bar; ``verified`` reflects the §2.3 hash
    outcome; ``error`` is set only in the ``failed`` terminal state."""

    model_config = ConfigDict(frozen=True)

    id: str
    item_id: str
    agent_id: str
    state: TransferState
    bytes_transferred: int = 0
    total_bytes: int | None = None
    verified: bool = False
    error: str | None = None


# --- share-location model: hint (P10-T11) + central mapping (P10-T12) -------

ShareScheme = Literal["unc", "url", "posix", "unknown"]

#: How an agent (or the central admin) obtained a share hint. The per-OS
#: discovery sources are best-effort (R1); ``central_mapping`` is the
#: deterministic P10-T12 fallback when none of them yield a hint.
ShareSource = Literal[
    "windows_net_share",  # `net share`
    "windows_wmi",  # Win32_Share
    "linux_smb_conf",  # smb.conf [share] sections
    "linux_exports",  # /etc/exports (NFS)
    "macos_sharing",  # `sharing -l`
    "central_mapping",  # admin-defined ShareMapping (P10-T12)
]


class ShareHint(BaseModel):
    """A best-effort network-share pointer an agent attaches to a replicated
    item (P10-T11). Rides the existing Phase-5 replication event shape as an
    **additive** field (``AgentEvent.share_hint``) — no new channel. Central
    stores it and renders a "network-open" link exactly as it does for a
    centrally-scanned item's ``library.share_prefix`` (pathlinks).

    ``share_url`` is the already-resolved openable location
    (``\\\\host\\share\\rel`` or ``smb://host/share/rel``); ``scheme`` is its
    :func:`classify_prefix` classification. ``best_effort`` is always True for
    agent-discovered hints (R1): its absence is not an error, and a stale hint
    never overrides the deterministic central mapping."""

    model_config = ConfigDict(frozen=True)

    share_url: str
    scheme: ShareScheme = "unknown"
    source: ShareSource
    host: str | None = None
    share_name: str | None = None
    best_effort: bool = True


class ShareMapping(BaseModel):
    """One admin-defined central share mapping (P10-T12), the deterministic
    fallback when an agent can't self-report a :class:`ShareHint`.

    Generalises ``library.share_prefix``: instead of one prefix per library, an
    operator maps a ``local_prefix`` (as the agent's OS sees the file) to a
    ``share_prefix`` (the network location that reaches it), optionally scoped to
    a single ``agent_id`` and/or ``library_id``. ``agent_id=None`` is a
    library-/global-level rule matching any agent. Resolution is
    longest-``local_prefix``-wins (:func:`resolve_share_url`), mirroring
    ``resolve_scan_path``. Intended DDL: ``agent_share_maps`` (tasks doc)."""

    model_config = ConfigDict(frozen=True)

    local_prefix: str
    share_prefix: str
    agent_id: str | None = None
    library_id: str | None = None
    # Optional pre-supplied Windows ``\\host\share`` counterpart of
    # ``share_prefix`` (UI-T15). When ``None`` the caller derives it from
    # ``share_prefix`` at render time (SMB only) — see
    # :func:`filearr.share_map.resolve_for_agent`.
    unc: str | None = None


# --- pure share-URL resolution (mirror of frontend pathlinks) --------------


def classify_prefix(prefix: str) -> ShareScheme:
    """Classify a share prefix into the shape :mod:`pathlinks` handles, so a
    server-computed hint and the browser agree. Server-side mirror of the
    frontend ``classifyPrefix``: ``\\\\host\\share`` → ``unc``; a URL scheme
    (``smb://`` / ``ftp://`` / ``file://``) → ``url``; a leading ``/`` →
    ``posix``; anything else → ``unknown``."""
    if not prefix:
        return "unknown"
    if prefix.startswith("\\\\"):
        return "unc"
    i = prefix.find("://")
    if i > 0 and prefix[0].isalpha() and all(
        c.isalnum() or c in "+.-" for c in prefix[:i]
    ):
        return "url"
    if prefix.startswith("/"):
        return "posix"
    return "unknown"


def _norm_local(path: str) -> str:
    """Normalise a local path for prefix comparison: backslashes → forward
    slashes, surrounding slashes stripped. Separator-safe so a Windows agent's
    ``C:\\media\\x`` and a posix ``/media/x`` both compare on ``/`` boundaries.
    Case is **preserved** (never lowercased) — matching stays case-sensitive,
    the same discipline as ``resolve_scan_path``."""
    return path.replace("\\", "/").strip("/")


def _join_share(share_prefix: str, remainder_segments: list[str]) -> str:
    """Join a resolved ``share_prefix`` with the leftover path segments using the
    prefix's NATIVE separator: backslashes for a UNC prefix, forward slashes for
    a URL/posix/other prefix (mirrors pathlinks ``buildDisplayPath``). Segments
    are preserved verbatim (case + characters); nothing is URL-encoded — this is
    a display/open path, not a percent-encoded href."""
    if classify_prefix(share_prefix) == "unc":
        base = share_prefix.rstrip("\\")
        rel = "\\".join(remainder_segments)
        return f"{base}\\{rel}" if rel else base
    base = share_prefix.rstrip("/")
    rel = "/".join(remainder_segments)
    return f"{base}/{rel}" if rel else base


def _select_mapping(
    mappings: list[ShareMapping], agent_id: str | None, local_path: str
) -> tuple[ShareMapping, list[str]] | None:
    """Pick the most specific covering :class:`ShareMapping` for ``local_path``
    (as agent ``agent_id`` sees it) and return it with the leftover path segments,
    or ``None`` when nothing covers it.

    The single, canonical share-map selection (R4 — no second path-matching
    scheme): longest-``local_prefix``-wins on **path-segment boundaries** (same
    geometry as ``resolve_scan_path``); a mapping covers ``local_path`` iff its
    ``local_prefix`` equals the path or is a parent directory of it. A mapping
    with ``agent_id=None`` matches any agent; one with a concrete ``agent_id``
    matches only that agent. Among covering mappings the longest ``local_prefix``
    wins; an agent-specific mapping and a global mapping of the *same* length
    resolve deterministically to the agent-specific one (it sorts first among
    equal-length finalists). Case-preserving + separator-safe (:func:`_norm_local`).
    Both :func:`resolve_share_url` (URL only) and
    :func:`filearr.share_map.resolve_for_agent` (URL + UNC) are thin wrappers of
    this, so a hint/mapping and the browser agree end to end. Pure; no I/O."""
    target = _norm_local(local_path)
    best: ShareMapping | None = None
    best_len = -1
    best_agent_specific = False
    for m in mappings:
        if m.agent_id is not None and m.agent_id != agent_id:
            continue
        base = _norm_local(m.local_prefix)
        covers = target == base or (base == "") or target.startswith(base + "/")
        if not covers:
            continue
        agent_specific = m.agent_id is not None
        # Longest prefix wins; at equal length prefer an agent-specific rule.
        if len(base) > best_len or (
            len(base) == best_len and agent_specific and not best_agent_specific
        ):
            best, best_len, best_agent_specific = m, len(base), agent_specific
    if best is None:
        return None
    base = _norm_local(best.local_prefix)
    target_segments = [s for s in target.split("/") if s]
    base_segments = [s for s in base.split("/") if s]
    remainder = target_segments[len(base_segments):]
    return best, remainder


def resolve_share_url(
    mappings: list[ShareMapping], agent_id: str | None, local_path: str
) -> str | None:
    """Resolve ``local_path`` (as agent ``agent_id`` sees it) to a share URL via
    the most specific covering :class:`ShareMapping`, or ``None`` if none covers
    it (caller then shows no network-open link).

    Thin URL-only wrapper of :func:`_select_mapping` (see it for the full
    longest-prefix / agent-vs-global precedence semantics). The intended runtime
    caller resolves at item display time, exactly where ``library.share_prefix``
    is read today; :func:`filearr.share_map.resolve_for_agent` is the both-format
    (URL + UNC) sibling. Pure and total: no I/O, no DB."""
    sel = _select_mapping(mappings, agent_id, local_path)
    if sel is None:
        return None
    best, remainder = sel
    return _join_share(best.share_prefix, remainder)


# --- transfer lifecycle state machine (brief §5, §6) -----------------------

TransferEvent = Literal[
    "start_upload",  # agent picked up the stage_upload command, bytes flowing
    "staged",  # upload complete + hash-verified, ready for download
    "download",  # browser Range-GET drained the staged file
    "expire",  # TTL lapsed (offline_timeout or idle staging sweep)
    "fail",  # transfer/verification error
]

# (current_state, event) -> next_state. Absent keys are invalid transitions and
# raise ValueError (terminal states pending/uploading/staged accept only their
# listed events; downloaded/expired/failed accept nothing).
_TRANSITIONS: dict[tuple[TransferState, TransferEvent], TransferState] = {
    ("pending", "start_upload"): "uploading",
    ("pending", "expire"): "expired",
    ("pending", "fail"): "failed",
    ("uploading", "staged"): "staged",
    ("uploading", "expire"): "expired",
    ("uploading", "fail"): "failed",
    ("staged", "download"): "downloaded",
    ("staged", "expire"): "expired",
    ("staged", "fail"): "failed",
}


def transfer_state_machine(current: TransferState, event: TransferEvent) -> TransferState:
    """Advance the retrieve lifecycle one step (brief §5/§6 states). PURE.

    Legal path: ``pending → uploading → staged → downloaded``. From any
    non-terminal state ``expire`` → ``expired`` and ``fail`` → ``failed``.
    ``downloaded`` / ``expired`` / ``failed`` are terminal. Any ``(current,
    event)`` not in the table (a terminal state, or an out-of-order event such
    as ``download`` before ``staged``) raises ``ValueError`` — invalid
    transitions are surfaced, never silently absorbed."""
    try:
        return _TRANSITIONS[(current, event)]
    except KeyError:
        raise ValueError(
            f"invalid transfer transition: {current!r} --{event!r}-->"
        ) from None


# --- content-addressed staging path (traversal-proof by construction) ------

#: Writable, non-media-mount staging root (brief §2: ordinary central disk, NOT
#: a read-only media mount, so invariant 6 is untouched). Under /config so it
#: shares the app's existing persistent volume.
STAGING_ROOT = "/config/staging"


def staging_path_for(transfer_id: str) -> str:
    """Return the on-disk staging path for a transfer, content-addressed by its
    id (brief §2.5 / §6.2 staging lifecycle).

    Traversal-proof **by construction**: ``transfer_id`` is parsed as a UUID and
    re-serialised, so the filename is guaranteed to be ``[0-9a-f-]`` only — a
    caller cannot smuggle ``..``, a path separator, an absolute path, or any
    other component that would escape :data:`STAGING_ROOT`. A non-UUID id raises
    ``ValueError`` (rejected before any filesystem touch). Returns a POSIX path
    string under :data:`STAGING_ROOT`."""
    canonical = str(uuid.UUID(str(transfer_id)))  # ValueError if not a UUID
    return str(PurePosixPath(STAGING_ROOT) / f"{canonical}.bin")


# --- Stateful pieces: stubs, implemented by the tagged Phase-10 task --------


def initiate_transfer(*_args: Any, **_kwargs: Any) -> Any:
    """P10-T13/T6: RBAC-``download``-gated retrieve initiation — create the
    ``agent_commands(kind='stage_upload')`` row + a ``staging_transfers`` row and
    return the transfer id (brief §4.1, §6). Authorization MUST run *before* this
    (no agent bandwidth / central disk spent for a principal lacking
    ``download``); the completed retrieve audits unconditionally (R2)."""
    raise NotImplementedError("P10-T13: RBAC-gated transfer initiation")


def stage_receiver(*_args: Any, **_kwargs: Any) -> Any:
    """P10-T4: the tus (or offset-``PATCH`` subset) staging-upload endpoint the
    agent drains bytes into — resumable, per-agent token-bucket rate limited,
    with agent-side path re-validation against its own roots before any read
    (brief §2.2/§2.4, §4 defense-in-depth). Streaming hash verify on completion
    is P10-T5."""
    raise NotImplementedError("P10-T4: tus staging-upload receiver")


def stream_staged(*_args: Any, **_kwargs: Any) -> Any:
    """P10-T6: Range-capable ``GET`` that streams a verified staged file to the
    browser (``Content-Disposition`` attachment), watermarking
    ``last_range_request_at`` so the TTL sweep (P10-T8) won't reap a file mid
    download. Gated by RBAC ``download`` + an unconditional audit line (R2)."""
    raise NotImplementedError("P10-T6: staged-file Range download stream")
