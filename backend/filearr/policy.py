"""Agent config/policy resolution + validation (Phase 5, P5-T6).

The pure-ish core behind the config-push channel (research §6):

* :func:`parse_scope` / :func:`scope_string` — the frozen scope-string grammar
  ``global`` | ``group:<name>`` | ``agent:<uuid>`` ↔ ``(scope_type, scope_id)``.
* :class:`PolicyModel` / :func:`validate_policy` — additive v1 validation of the
  known policy keys (unknown keys are PRESERVED verbatim — an older central must
  never strip a newer agent's keys; §6.3).
* :func:`resolve_effective_policy` — the most-specific-wins resolution across the
  precedence ``agent`` > ``group`` > ``global`` (NO merging in v1: the winning
  row IS the policy; merging is a documented future option).

The HTTP surface (agent poll + admin write) lives in
:mod:`filearr.api.agent_policies`; this module holds the logic so it is unit-
testable without a request.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import rbac
from filearr.models import PolicyVersion
from filearr.presets import validate_preset_names

#: Offline-grace default for the local query surface (P7-T4 / research §5.2, R4).
#: This REUSES Phase-5's 24h reconciliation threshold — the same 24h value the Go
#: agent carries as ``config.DefaultOfflineGrace`` (== ``defaultReconcileInterval``
#: in ``agent/cmd/filearr-agent/config.go``). It is deliberately NOT a second
#: constant: past this window with no fresh policy, the agent web UI fails closed
#: while the CLI same-user path keeps answering. The operator may override it per
#: policy via ``offline_grace_seconds``.
DEFAULT_OFFLINE_GRACE_SECONDS = 86400

#: Upper bound on the number of flattened path-scope predicates a policy may carry
#: (payload-size guard; the whole policy is additionally 64KiB-capped at the API).
MAX_PATH_SCOPE_PREDICATES = 1000


# --------------------------------------------------------------------------- #
# Scope grammar                                                                #
# --------------------------------------------------------------------------- #
class ScopeError(ValueError):
    """Malformed scope string (maps onto a 422 at the API)."""


def parse_scope(scope: str) -> tuple[str, str | None]:
    """``global`` | ``group:<name>`` | ``agent:<uuid>`` → ``(scope_type, scope_id)``.

    Raises :class:`ScopeError` on anything malformed (empty group name, a
    non-UUID agent id, an unknown prefix). ``global`` → ``("global", None)``;
    ``group:x`` → ``("group", "x")``; ``agent:<uuid>`` → ``("agent", "<uuid>")``
    (the UUID is normalised to its canonical lowercase text form)."""
    if scope == "global":
        return "global", None
    prefix, sep, rest = scope.partition(":")
    if not sep:
        raise ScopeError(f"malformed scope: {scope!r}")
    if prefix == "group":
        if not rest:
            raise ScopeError("group scope requires a non-empty name")
        return "group", rest
    if prefix == "agent":
        try:
            aid = uuid.UUID(rest)
        except (ValueError, AttributeError) as err:
            raise ScopeError(f"agent scope requires a UUID: {rest!r}") from err
        return "agent", str(aid)
    raise ScopeError(f"unknown scope kind: {prefix!r}")


def scope_string(scope_type: str, scope_id: str | None) -> str:
    """Inverse of :func:`parse_scope`."""
    if scope_type == "global":
        return "global"
    return f"{scope_type}:{scope_id}"


# --------------------------------------------------------------------------- #
# Policy JSON validation (additive; unknown keys preserved)                    #
# --------------------------------------------------------------------------- #
class PolicyValidationError(ValueError):
    """A policy body that fails v1 validation (maps onto a 422 at the API)."""


class PolicyModel(BaseModel):
    """Validation gate for the KNOWN v1 policy keys — all optional. Unknown keys
    are allowed (``extra='allow'``) and PRESERVED (the row stores the ORIGINAL
    dict verbatim; this model only validates). Bounds mirror the frozen contract:
    presets against ``PRESET_BUNDLES``; ``content_hash_max_bytes >= 0``;
    ``reconcile_interval_seconds >= 300``; ``poll_interval_seconds`` 60..86400.

    P7-T4 adds the local-query-surface keys the Go agent consumes
    (``agent/internal/config``, research §5):

    * ``local_access_enabled`` (bool) — gates the CLI/local-API listener. Absent =
      agent default ON (a never-contacted agent keeps the CLI enabled). An explicit
      ``false`` persists through offline periods (it is cached).
    * ``web_ui_enabled`` (bool) — gates the local web UI (P7-T5). Absent = agent
      default OFF (a never-contacted agent starts web-UI-disabled). Fails closed
      when the cached policy is older than ``offline_grace_seconds``.
    * ``auth_required`` (bool) — whether the web UI demands the bootstrap token;
      absent = agent default ON. Never affects the CLI peer-credential check.
    * ``read_only`` (bool) — ALWAYS ``true``. The local surface is read-only by
      invariant; a ``false`` is REJECTED here (fail-closed, security > all).
    * ``path_scope`` (list[str]) — the FLATTENED allow-list of ``rel_path`` GLOB
      predicates the agent applies as ``WHERE rel_path GLOB ?`` (OR-combined) to
      every local result set (R2: the agent consumes flattened predicates only, it
      never grows a rule evaluator). Operator-authored, or produced by
      :func:`flatten_path_grants` from RBAC grants. Empty/absent = unrestricted.
    * ``offline_grace_seconds`` (int) — the web-UI fail-closed grace window; absent
      = :data:`DEFAULT_OFFLINE_GRACE_SECONDS` (24h, R4).
    """

    model_config = ConfigDict(extra="allow")

    presets: list[str] | None = None
    include_globs: list[str] | None = None
    exclude_globs: list[str] | None = None
    content_hash_max_bytes: int | None = Field(default=None, ge=0)
    watch_mode: bool | None = None
    reconcile_interval_seconds: int | None = Field(default=None, ge=300)
    poll_interval_seconds: int | None = Field(default=None, ge=60, le=86400)

    # --- P7-T4 local query surface -----------------------------------------
    local_access_enabled: bool | None = None
    web_ui_enabled: bool | None = None
    auth_required: bool | None = None
    read_only: bool | None = None
    path_scope: list[str] | None = None
    offline_grace_seconds: int | None = Field(default=None, ge=0)

    # --- P10-T4 agent staging-upload rate cap ------------------------------
    # ``upload_rate_bytes_per_sec`` (int >= 0) — the per-agent token-bucket
    # ceiling the Go agent applies to a ``stage_upload`` (research §2.4). 0 or
    # absent = UNLIMITED. The agent reads the cached value at upload START; a
    # mid-upload policy change takes effect on the NEXT upload (documented). This
    # is additive — the P7-T4 keys and their tests are untouched.
    upload_rate_bytes_per_sec: int | None = Field(default=None, ge=0)

    @field_validator("presets")
    @classmethod
    def _known_presets(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            unknown = validate_preset_names(v)
            if unknown:
                raise ValueError(f"unknown preset(s): {', '.join(sorted(unknown))}")
        return v

    @field_validator("read_only")
    @classmethod
    def _read_only_is_true(cls, v: bool | None) -> bool | None:
        # The local surface is read-only by invariant (research §3.4). Reject any
        # attempt to disable it rather than silently normalize — an operator asking
        # for a writable local surface is a policy error, not a preference.
        if v is False:
            raise ValueError(
                "read_only cannot be disabled — the local query surface is always read-only"
            )
        return v

    @field_validator("path_scope")
    @classmethod
    def _valid_path_scope(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if len(v) > MAX_PATH_SCOPE_PREDICATES:
            raise ValueError(
                f"path_scope has {len(v)} predicates; max {MAX_PATH_SCOPE_PREDICATES}"
            )
        for i, pred in enumerate(v):
            if not isinstance(pred, str) or not pred.strip():
                raise ValueError(f"path_scope[{i}] must be a non-empty glob string")
        return v


def validate_policy(policy: Any) -> None:
    """Validate the KNOWN v1 keys of ``policy`` (unknown keys pass through).

    Raises :class:`PolicyValidationError` when ``policy`` is not a JSON object or
    a known key violates its bound. The caller stores the ORIGINAL ``policy``
    verbatim — this is a gate, not a transform."""
    if not isinstance(policy, dict):
        raise PolicyValidationError("policy must be a JSON object")
    try:
        PolicyModel(**policy)
    except ValidationError as err:
        raise PolicyValidationError(_summarise(err)) from err


def _summarise(err: ValidationError) -> str:
    parts = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e.get("loc", ())) or "policy"
        parts.append(f"{loc}: {e.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid policy"


def policy_json_len(policy: Any) -> int:
    """Compact-JSON byte length of a policy (the oversize gate's measure)."""
    return len(
        json.dumps(policy, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


# --------------------------------------------------------------------------- #
# Effective-policy resolution (most-specific-wins; agent > group > global)      #
# --------------------------------------------------------------------------- #
async def _max_version_row(
    session: AsyncSession, scope_type: str, scope_id: str | None
) -> PolicyVersion | None:
    """The highest-version row for one scope, or None (the CURRENT policy there)."""
    q = (
        select(PolicyVersion)
        .where(
            PolicyVersion.scope_type == scope_type,
            PolicyVersion.scope_id.is_(scope_id)
            if scope_id is None
            else PolicyVersion.scope_id == scope_id,
        )
        .order_by(PolicyVersion.version.desc())
        .limit(1)
    )
    return (await session.execute(q)).scalars().first()


async def next_version(
    session: AsyncSession, scope_type: str, scope_id: str | None
) -> int:
    """The version a NEW row for this scope takes: prior scope max + 1 (1 if none)."""
    row = await _max_version_row(session, scope_type, scope_id)
    return (row.version + 1) if row is not None else 1


async def resolve_effective_policy(
    session: AsyncSession, agent: Any
) -> tuple[str, int, dict]:
    """Resolve the effective policy for ``agent`` — most-specific-wins across the
    precedence ``agent:<id>`` > ``group:<rollout_group>`` > ``global`` (NO
    merging: the winning row's policy IS the answer). Returns
    ``(scope_string, version, policy)``; with no policy rows at all,
    ``("none", 0, {})`` (an agent must never 404 the policy endpoint)."""
    for scope_type, scope_id in (
        ("agent", str(agent.id)),
        ("group", agent.rollout_group),
        ("global", None),
    ):
        row = await _max_version_row(session, scope_type, scope_id)
        if row is not None:
            return scope_string(scope_type, scope_id), row.version, row.policy
    return "none", 0, {}


# --------------------------------------------------------------------------- #
# RBAC grant → flattened path-scope predicate list (R2, best-effort)           #
# --------------------------------------------------------------------------- #
class PathScopeFlattenError(ValueError):
    """A grant set that cannot be safely flattened into an OR-combined GLOB
    allow-list (a deny grant, or an irreversible hashed ltree label). Raised
    FAIL-CLOSED: central must never emit an over-broad scope in these cases —
    the operator should author the ``path_scope`` list explicitly instead."""


# A leading ``lib_<uuid.hex>`` ltree label (see :func:`rbac.library_label`).
_LIB_LABEL_RE = re.compile(r"^lib_[0-9a-f]{32}$")


def flatten_path_grants(
    grants: list[rbac.PathGrant],
    *,
    read_action: str = "search_metadata",
    strip_library_prefix: bool = True,
) -> list[str]:
    """Flatten a principal's RBAC path grants into the ``path_scope`` predicate
    list the agent applies locally (R2 — the agent never evaluates rules).

    This is a **minimal, best-effort** helper for the clean subset that maps onto
    an OR-combined ``rel_path`` GLOB allow-list. The local surface is read-only, so
    only grants for ``read_action`` (default ``search_metadata``) are relevant. For
    each such ALLOW grant the ltree-encoded ``PathGrant.path`` is decoded back to a
    ``rel_path`` and emitted as two globs — the exact path and its subtree
    ``<rel_path>/**`` — so both a file grant and a directory grant are covered.
    A library-root grant (only the ``lib_<uuid>`` label) becomes ``**`` (the whole
    library subtree).

    **Documented gaps** (grants do NOT map cleanly in these cases — the function
    fail-closes rather than emit a wrong scope; author ``path_scope`` by hand):

    * **Deny grants.** An explicit deny (``allow=False``) for ``read_action``
      cannot be expressed in a pure OR-allow list — it would require a local rule
      evaluator, which R2 forbids. Raises :class:`PathScopeFlattenError`.
    * **The library dimension is dropped.** The ``lib_<uuid>`` prefix is stripped
      (``strip_library_prefix``): the agent index is per-machine with its own roots
      and no central library_id, so multi-library grants collapse onto one
      rel_path space. A per-machine agent typically maps to one library's roots;
      push the right scope to the right rollout group.
    * **Hashed (over-long) ltree labels** are one-way (:data:`rbac.HASHED_LABEL`)
      and cannot be turned back into a glob. Raises :class:`PathScopeFlattenError`.
    * **GLOB metacharacters in a literal directory name** (a real dir literally
      named with ``*``/``?``/``[``) are not escaped — rare; author by hand if hit.
    """
    read_grants = [g for g in grants if g.action == read_action]
    predicates: set[str] = set()
    for g in read_grants:
        if not g.allow:
            raise PathScopeFlattenError(
                "cannot flatten a deny grant into an OR-allow path_scope list "
                f"(path={g.path!r}); author path_scope explicitly"
            )
        rel = _ltree_to_rel_path(g.path, strip_library_prefix=strip_library_prefix)
        if rel == "":
            predicates.add("**")  # whole (library) subtree
        else:
            predicates.add(rel)
            predicates.add(f"{rel}/**")
    return sorted(predicates)


def _ltree_to_rel_path(path: str, *, strip_library_prefix: bool) -> str:
    """Decode an ltree grant path back to a ``rel_path`` (empty = library root)."""
    labels = path.split(".")
    if strip_library_prefix and labels and _LIB_LABEL_RE.match(labels[0]):
        labels = labels[1:]
    segments: list[str] = []
    for label in labels:
        seg = rbac.decode_path_label(label)
        if seg == rbac.HASHED_LABEL:
            raise PathScopeFlattenError(
                f"grant path {path!r} contains a one-way hashed ltree label; "
                "author path_scope explicitly"
            )
        segments.append(seg)
    return "/".join(segments)
