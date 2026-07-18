"""Meilisearch operational helpers (Phase 9, roadmap §8 — Meili feature adoption).

**Inert scaffolding.** Only tests import this module; nothing in the runtime wires
it in yet. It ships the *pure* logic behind the phase-9 operational features
(fragmentation-driven compaction, shadow-index swap naming, settings drift
detection, webhook-secret verification) plus the target settings/typo-tolerance
*specification* that ``search.py``'s ``ensure_index()`` will consume once the
tasks below land. The Meili-client-touching entry points are typed STUBS tagged
with their owning task (``compact_if_fragmented`` P9-T4, ``rebuild_via_swap``
P9-T5, ``ensure_webhook`` P9-T6).

Design constraint (whole module): **no Meilisearch client calls, no network, no
ORM** — every function here is pure and unit-testable so the implementing tasks
inherit green coverage of the decision logic and only have to wire the async I/O.
The disposable-index invariant (invariant 1) is preserved: nothing here makes
Meili a store of record; the swap/compaction/reconciliation helpers are all
projections of Postgres truth.

SDK method names referenced by the STUB docstrings were verified against the
installed ``meilisearch-python-sdk==7.2.3`` at scaffold time (brief §7 Q2 / R5):
``AsyncIndex.compact()``, ``AsyncIndex.get_stats()`` (``database_size`` /
``used_database_size``), ``AsyncIndex.update_search_cutoff_ms()``,
``AsyncIndex.facet_search()``, ``AsyncClient.swap_indexes(list[tuple[str,str]])``,
``AsyncClient.create_index()`` / ``delete_index_if_exists()``,
``AsyncClient.get_webhooks()`` / ``create_webhook(WebhookCreate)``. All present.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from filearr.hashx import HASH_ATTRIBUTES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Target index-settings specification (consumed later by ensure_index()).
#
# These lists mirror the live projection in ``search.py`` but are stated here as
# the phase-9 *target* shape (adds facet-search opt-outs, an extended typo
# disable-list, and a search-cutoff guard). Keys use Meili's camelCase settings
# names so ``settings_drift`` can diff them directly against a ``GET /settings``
# response.
# ---------------------------------------------------------------------------

# NOTE: attribute ORDER is meaningful for ``searchableAttributes`` (it feeds the
# attribute-ranking rule) and for ``rankingRules`` — both listed in
# ``ORDERED_LIST_SETTINGS``. All other list-valued settings are unordered SETS as
# far as Meilisearch is concerned (``filterableAttributes``, ``sortableAttributes``,
# ``stopWords``, and typo ``disableOnAttributes``): Meili stores/returns them
# without preserving caller order, so a drift check MUST compare them set-wise or
# it will report phantom drift on every boot.
SEARCHABLE_ATTRIBUTES: tuple[str, ...] = (
    "title", "filename", "path", "artist", "album", "author", "tags",
    # P3-T5 document body text — LAST so filename/title/tags outrank a body match
    # via the attribute-ranking rule (a hit whose NAME matches beats one whose
    # deep body text matches). Order is significant here (searchableAttributes is
    # in ORDERED_LIST_SETTINGS), so this append is a genuine settings change.
    "body_text",
    # P3-T13: flat newline-joined archive member names ("which archive CONTAINS a
    # file called X"). LAST, after body_text, so a name/title/body hit outranks an
    # archive-member match via the attribute-ranking rule. Order is significant
    # (searchableAttributes is in ORDERED_LIST_SETTINGS) so this append is a
    # genuine settings change; build_doc projects it from metadata_.archive_members.
    "archive_members",
)
FILTERABLE_ATTRIBUTES: tuple[str, ...] = (
    "media_type", "library_id", "status", "extension", "year", "tags",
    "codec", "resolution", "genre", "size", "mtime", "is_sidecar", "sidecar_of",
    # P3-T1 hash search: the scan-time xxh3 digests become exact-match filter
    # targets (typo tolerance already off for both — they are HASH_ATTRIBUTES).
    # Facet search is explicitly DISABLED for both (see FACET_SEARCH_DISABLED):
    # they are near-unique opaque digests, so equality/comparison filtering is all
    # P3-T1 needs and a facet-search index over them would be pure bloat.
    "quick_hash", "content_hash",
    # P6-T3 RBAC search scoping: the ARRAY of ltree ancestor labels covering each
    # item (``tenant_tokens.PATH_SCOPE_ATTR``). Filterable so the server-side proxy
    # can ``AND`` a deny-aware ``path_scope IN [...]`` scope filter into every
    # search query; never facet-searched (a scope key, not a human facet — see
    # FACET_SEARCH_DISABLED). Adding it is genuine settings drift → a rebuild-index
    # is required after deploy to project it onto existing docs (ops runbook).
    "path_scope",
)
# ``mtime_sort`` (FIX-3): a CLAMPED copy of ``mtime`` (min(mtime, index-time)) used
# as the sort key for sort=newest so a bogus FUTURE mtime cannot float to the top.
# Raw ``mtime`` stays sortable too (explicit sort=mtime:asc|desc / range filters).
SORTABLE_ATTRIBUTES: tuple[str, ...] = (
    "title", "year", "size", "mtime", "mtime_sort", "recency_bucket",
)

# Numeric fields where fuzzy matching is nonsensical (brief §2e). The string /
# hash / UUID-shaped members come from the SINGLE SOURCE OF TRUTH in
# ``filearr.hashx.HASH_ATTRIBUTES`` (extension / mtime / sidecar_of today; quick_hash
# / content_hash join automatically when P3-T1 makes them filterable) so the two
# briefs cannot drift apart. Sorted for deterministic settings comparison.
_NUMERIC_NO_TYPO: tuple[str, ...] = ("year", "size")
DISABLE_TYPO_ATTRIBUTES: tuple[str, ...] = tuple(
    sorted(set(HASH_ATTRIBUTES) | set(_NUMERIC_NO_TYPO))
)

# Filterable attributes that must NEVER be facet-searchable (brief §2c): faceting
# on a near-unique field is meaningless and wastes index-time work (Meili builds a
# per-value facet-search structure for every facet-searchable attribute). The
# genuine facet-search candidates (tags/genre/media_type/is_sidecar) keep the
# default (enabled) and are intentionally absent here.
#
# Queued follow-up DECISION (hash facet-search): ``quick_hash``/``content_hash``
# became FILTERABLE in P3-T1 for exact-match hash lookup. Exact-match / equality
# filtering does NOT use facet search (it is a plain ``field = "<hex>"`` filter),
# so facet search over these fields buys nothing — and they are the highest-
# cardinality attributes in the projection (a distinct opaque digest per file), so
# a facet-search index over them is pure bloat with zero human utility (no one
# type-aheads a 16/32-char xxh3 hex). They are therefore facet-search-DISABLED
# here while REMAINING filterable (P3-T1 exact-match search is unaffected —
# ``search.py._filterable_settings`` keeps ``filter=equality+comparison`` for
# every attribute and only flips ``features.facet_search`` off for this set).
FACET_SEARCH_DISABLED: tuple[str, ...] = (
    "size", "mtime", "year", "path_scope", "quick_hash", "content_hash",
)

# The genuine facet-search CANDIDATES (brief §2c): low-cardinality,
# human-meaningful fields worth a type-ahead. They KEEP facet search enabled
# (absent from FACET_SEARCH_DISABLED) and, per R2, get their facet VALUES
# count-ordered (most-common first) — the tag type-ahead consumer is P3-T12.
FACET_SEARCH_CANDIDATES: tuple[str, ...] = ("tags", "genre", "media_type", "is_sidecar")

# searchCutoffMs circuit-breaker (brief §2f): generous but non-infinite so a
# pathological filter/query cannot hang a request. SDK method verified:
# ``AsyncIndex.update_search_cutoff_ms(1500)``.
DEFAULT_SEARCH_CUTOFF_MS: int = 1500

# Fragmentation ratio above which ``/compact`` is worthwhile (brief §3.1, Meili's
# own ~1.3 guidance).
DEFAULT_COMPACTION_THRESHOLD: float = 1.3


@dataclass(frozen=True)
class TypoToleranceSpec:
    """Target ``typoTolerance`` shape (typed, not a dict — CLAUDE.md gotcha).

    ``disable_on_attributes`` is a SET as far as Meili is concerned; kept as a
    sorted tuple here for deterministic comparison and reproducible settings
    application.
    """

    enabled: bool = True
    disable_on_attributes: tuple[str, ...] = DISABLE_TYPO_ATTRIBUTES

    def as_meili(self) -> dict[str, object]:
        """Render to the camelCase dict shape Meili's ``/settings`` returns."""
        return {
            "enabled": self.enabled,
            "disableOnAttributes": list(self.disable_on_attributes),
        }


TYPO_TOLERANCE_SPEC = TypoToleranceSpec()

# P3-T2 recency ranking: Meili's six built-in ranking rules, in their default
# order, with a custom ``recency_bucket:asc`` rule appended AFTER ``exactness``.
# Placement matters (rankingRules is order-significant): recency acts purely as a
# TIE-BREAKER once relevance (words -> typo -> proximity -> attribute -> sort ->
# exactness) has already ordered the hits, so a recently-touched file wins ties
# without ever starving relevance. ``:asc`` because ``recency_bucket`` counts UP
# with age (0 = <7d ... 4 = older), so ascending surfaces the freshest bucket
# first. Bucketed (not raw epoch) on purpose: a raw-timestamp custom rule would
# dominate every earlier rule; a coarse 5-bucket integer only breaks genuine ties.
DEFAULT_RANKING_RULES: tuple[str, ...] = (
    "words", "typo", "proximity", "attribute", "sort", "exactness",
)
RECENCY_RANKING_RULE: str = "recency_bucket:asc"
RANKING_RULES: tuple[str, ...] = (*DEFAULT_RANKING_RULES, RECENCY_RANKING_RULE)


# Meili setting keys whose list VALUE is order-significant. Every other list-valued
# setting is compared as an unordered set by ``settings_drift``.
ORDERED_LIST_SETTINGS: frozenset[str] = frozenset(
    {"rankingRules", "searchableAttributes"}
)

# The full desired settings projection (plain data; ensure_index() applies it).
INDEX_SETTINGS_SPEC: dict[str, object] = {
    "searchableAttributes": list(SEARCHABLE_ATTRIBUTES),
    "filterableAttributes": list(FILTERABLE_ATTRIBUTES),
    "sortableAttributes": list(SORTABLE_ATTRIBUTES),
    "rankingRules": list(RANKING_RULES),
    "typoTolerance": TYPO_TOLERANCE_SPEC.as_meili(),
    "searchCutoffMs": DEFAULT_SEARCH_CUTOFF_MS,
    # Per-attribute facet-search opt-outs; ensure_index() translates this into the
    # filterableAttributes object-form (features.facetSearch=false) at apply time.
    "facetSearchDisabled": list(FACET_SEARCH_DISABLED),
}


# ---------------------------------------------------------------------------
# P3-T8 semantic-search embedder settings (userProvided source) — pure.
#
# We generate vectors ourselves (local ONNX; brief §1/§2) and push them on each
# document's ``_vectors`` block, so Meili's embedder is the ``userProvided``
# source: it stores + ANN-indexes vectors but never calls out to embed anything
# (no cloud, no in-Meili model). ``dimensions`` MUST match the model (bge-small =
# 384). Binary quantization is deliberately OFF for v1 (R2: dense-first; enable
# only once Hannoy-BQ is verified on the live version AND the corpus threatens
# memory). SDK model names verified against meilisearch-python-sdk==7.2.3
# (``Embedders`` / ``UserProvidedEmbedder(source="userProvided", dimensions=...)``).
# ---------------------------------------------------------------------------
DEFAULT_EMBEDDER_NAME: str = "default"


def build_embedders(dim: int, *, name: str = DEFAULT_EMBEDDER_NAME):
    """Return the typed ``Embedders`` settings for a single ``userProvided``
    embedder of ``dim`` dimensions (SDK typed models, never dicts — CLAUDE.md
    gotcha). Applied ONLY when semantic search is enabled (drift-safe: an install
    with semantic off never carries an embedder in its settings)."""
    from meilisearch_python_sdk.models.settings import Embedders, UserProvidedEmbedder

    return Embedders(
        embedders={name: UserProvidedEmbedder(source="userProvided", dimensions=dim)}
    )


def embedder_matches(current, dim: int, *, name: str = DEFAULT_EMBEDDER_NAME) -> bool:
    """True when the live ``Embedders`` already has ``name`` at ``dim`` dimensions
    (idempotency guard so ``_apply_settings`` doesn't re-push an unchanged embedder
    every boot). A missing embedders block / missing name / wrong dim → False."""
    if current is None:
        return False
    embedders = getattr(current, "embedders", None) or {}
    emb = embedders.get(name)
    if emb is None:
        return False
    return getattr(emb, "dimensions", None) == dim


# ---------------------------------------------------------------------------
# Fragmentation / compaction decision (brief §3.1) — pure.
# ---------------------------------------------------------------------------
def fragmentation_ratio(
    database_size: int | float | None,
    used_database_size: int | float | None,
) -> float:
    """``database_size / used_database_size`` from ``GET /stats``.

    None-tolerant and division-by-zero-guarded: if either input is missing or
    ``used_database_size`` is non-positive the fragmentation is unmeasurable, so
    return ``0.0`` (which ``should_compact`` reads as "nothing to reclaim"), never
    raise. A healthy, unfragmented index reports ~1.0.
    """
    if database_size is None or used_database_size is None:
        return 0.0
    if used_database_size <= 0:
        return 0.0
    return database_size / used_database_size


def should_compact(
    ratio: float, threshold: float = DEFAULT_COMPACTION_THRESHOLD
) -> bool:
    """True when fragmentation exceeds ``threshold`` (default 1.3, brief §3.1).

    Strict ``>``: a ratio exactly at the threshold does not trigger the 2x-disk
    compaction cost. The unmeasurable ``0.0`` sentinel from
    ``fragmentation_ratio`` is safely below any sane threshold, so a missing stat
    never triggers a spurious compaction.
    """
    return ratio > threshold


# ---------------------------------------------------------------------------
# Shadow-index naming for the swap-based rebuild (brief §2b) — pure.
# ---------------------------------------------------------------------------
_SHADOW_SEP = "_rebuild_"


def _to_epoch(ts: datetime | int | float) -> int:
    """Coerce a datetime or epoch-ish value to integer epoch seconds."""
    if isinstance(ts, datetime):
        return int(ts.timestamp())
    return int(ts)


def shadow_uid(base_uid: str, ts: datetime | int | float) -> str:
    """Deterministic shadow-index name embedding a rebuild timestamp.

    ``f"{base_uid}_rebuild_{epoch_seconds}"``. Deterministic (unlike the brief's
    random-suffix sketch) so ``is_stale_shadow`` can recover the age from the name
    alone — a crashed rebuild's orphaned index is identifiable and reap-able by a
    periodic sweep without any external bookkeeping (brief §2b cleanup note).
    """
    return f"{base_uid}{_SHADOW_SEP}{_to_epoch(ts)}"


def parse_shadow_ts(uid: str) -> int | None:
    """Recover the embedded epoch from a shadow uid, or None if not one.

    Uses the LAST ``_rebuild_`` separator so a ``base_uid`` that itself contains
    the separator round-trips. A non-numeric tail (or no separator) yields None,
    so a real primary index name is never mistaken for a shadow.
    """
    _, sep, tail = uid.rpartition(_SHADOW_SEP)
    if not sep or not tail.isdigit():
        return None
    return int(tail)


def is_shadow_uid(uid: str) -> bool:
    """True when ``uid`` matches the ``shadow_uid`` naming scheme."""
    return parse_shadow_ts(uid) is not None


def is_stale_shadow(
    uid: str,
    now: datetime | int | float,
    max_age: timedelta | int | float,
) -> bool:
    """True when ``uid`` is a shadow index older than ``max_age``.

    Non-shadow names return False (never reaped). ``now`` may be a datetime or
    epoch; ``max_age`` a timedelta or a number of seconds. Round-trips with
    ``shadow_uid``: ``is_stale_shadow(shadow_uid(b, t), t + max_age + 1, max_age)``
    is True, and ``... t, max_age)`` is False.
    """
    created = parse_shadow_ts(uid)
    if created is None:
        return False
    max_age_s = max_age.total_seconds() if isinstance(max_age, timedelta) else float(max_age)
    age = _to_epoch(now) - created
    return age > max_age_s


# ---------------------------------------------------------------------------
# Settings drift detection (brief §2b/§3.6 — ensure_index re-applies on boot).
# ---------------------------------------------------------------------------
def _normalize(value: object, *, ordered: bool) -> object:
    """Canonicalise a settings value for order-insensitive-where-appropriate diff.

    Lists/tuples become tuples when ``ordered`` (ranking rules, searchable attrs)
    and frozensets otherwise (Meili treats them as sets). Nested dicts recurse,
    re-deciding orderedness per nested key (so ``typoTolerance.disableOnAttributes``
    is compared set-wise even though its parent is a dict).
    """
    if isinstance(value, dict):
        return {
            k: _normalize(v, ordered=k in ORDERED_LIST_SETTINGS)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return tuple(value) if ordered else frozenset(value)
    return value


def settings_drift(current: dict, desired: dict) -> list[str]:
    """Return the sorted names of settings in ``desired`` that differ from ``current``.

    ``desired`` is the source of truth (``INDEX_SETTINGS_SPEC``); only its keys are
    checked (extra keys Meili returns are ignored). List-valued settings are
    compared as SETS except ``rankingRules`` / ``searchableAttributes`` which are
    order-sensitive; nested dicts (e.g. ``typoTolerance``) compare recursively with
    the same rule. A key missing from ``current`` counts as drifted.
    """
    drifted: list[str] = []
    for key, desired_val in desired.items():
        ordered = key in ORDERED_LIST_SETTINGS
        cur_norm = _normalize(current.get(key), ordered=ordered)
        des_norm = _normalize(desired_val, ordered=ordered)
        if cur_norm != des_norm:
            drifted.append(key)
    return sorted(drifted)


# Settings whose change requires REPROCESSING every document (a full shadow-swap
# rebuild) rather than an in-place ``ensure_index()`` apply. The simple rule for
# now (P9-T5): every managed setting today (``searchableAttributes`` /
# ``filterableAttributes`` / ``sortableAttributes`` / ``typoTolerance`` /
# ``faceting`` / ``searchCutoffMs``) is a pure INDEX-SIDE setting Meilisearch
# applies to the live index in place — none of them change how ``build_doc``
# derives a document — so this set is empty and bootstrap always stays in-place.
# It exists as the ONE place a future ``build_doc``-shape / schema migration (a
# change that only becomes consistent once every document is re-derived) flips a
# key to "rebuild required", so the in-place-vs-rebuild decision lives in code,
# not tribal knowledge. Deliberate settings migrations trigger the rebuild task
# (``POST /api/v1/system/rebuild-index`` or ``nightly_reconcile``); bootstrap and
# routine tweaks apply in place.
REBUILD_REQUIRING_SETTINGS: frozenset[str] = frozenset()


def needs_rebuild_for_settings(drift: list[str] | set[str]) -> bool:
    """True when a settings ``drift`` (from ``settings_drift``) contains a setting
    that can only be made consistent by REPROCESSING every document — i.e. a full
    shadow-swap rebuild, not an in-place ``ensure_index()`` apply.

    Documented rule (P9-T5): bootstrap and routine settings tweaks apply in place;
    only a deliberate schema/``build_doc`` migration needs a rebuild. Returns False
    for every managed setting today (``REBUILD_REQUIRING_SETTINGS`` is empty) —
    Meilisearch applies all of them to the live index in place.
    """
    return bool(set(drift) & REBUILD_REQUIRING_SETTINGS)


# ---------------------------------------------------------------------------
# Webhook receiver contract + secret verification (brief §2a) — pure.
# ---------------------------------------------------------------------------
def verify_webhook_secret(header_value: str | None, secret: str | None) -> bool:
    """Constant-time check of a Meili webhook ``Authorization`` header.

    Compares the full header (``"Bearer <secret>"``) with ``hmac.compare_digest``
    to avoid a timing side-channel on the shared secret. Missing/empty header or
    secret is a hard False (never a compare against an empty expected value).
    """
    if not header_value or not secret:
        return False
    expected = f"Bearer {secret}"
    return hmac.compare_digest(header_value, expected)


class MeiliTaskNotification(BaseModel):
    """One task object from a Meili webhook ndjson payload (brief §2a).

    Inert receiver contract — the future ``/api/internal/meili-webhook`` route
    parses each ndjson line into this. Tolerant by design (``extra="allow"``,
    everything optional) so a payload-shape change across a Meili version bump
    degrades to "unknown fields ignored" rather than a hard parse failure that
    would drop failure observability. Accepts both camelCase (wire) and snake_case
    (Python) field names.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    uid: int | None = None
    batch_uid: int | None = Field(default=None, alias="batchUid")
    index_uid: str | None = Field(default=None, alias="indexUid")
    status: str | None = None
    type: str | None = None
    canceled_by: int | None = Field(default=None, alias="canceledBy")
    details: dict | None = None
    duration: str | None = None
    enqueued_at: str | None = Field(default=None, alias="enqueuedAt")
    started_at: str | None = Field(default=None, alias="startedAt")
    finished_at: str | None = Field(default=None, alias="finishedAt")

    @property
    def failed(self) -> bool:
        """True for a Meili task in the terminal ``failed`` state."""
        return self.status == "failed"


@dataclass
class WebhookTarget:
    """Desired webhook registration (brief §2a) — plain data for ensure_webhook()."""

    url: str
    secret: str
    headers: dict[str, str] = field(default_factory=dict)

    def auth_headers(self) -> dict[str, str]:
        """Headers to register with Meili, including the bearer auth."""
        return {"Authorization": f"Bearer {self.secret}", **self.headers}


# ---------------------------------------------------------------------------
# Meili-client-touching entry points — STUBS (each owns a task). No client calls
# land in this scaffolding pass; the pure decision logic above is what they wire.
# ---------------------------------------------------------------------------
async def compact_if_fragmented() -> bool:
    """P9-T4: weekly periodic — compact the index iff fragmented past threshold.

    Wiring: ``get_stats()`` → ``fragmentation_ratio`` → ``should_compact`` →
    ``AsyncIndex.compact()``; returns whether a compaction was triggered.
    """
    raise NotImplementedError("P9-T4: compact_if_fragmented periodic task")


async def rebuild_via_swap(*, wait_s: float | None = None) -> int:
    """P9-T5: full re-projection into a shadow index + atomic swap.

    Rebuilds the entire search projection from Postgres truth into a *fresh shadow
    index*, then atomically swaps it into place so a concurrent search NEVER sees a
    half-built index (invariant 1: the projection is disposable; Postgres is never
    written here). Returns the number of documents indexed.

    Flow (all Meili-side; SDK names verified against ``meilisearch-python-sdk``
    7.2.3 — ``create_index`` / ``swap_indexes`` / ``wait_for_task`` /
    ``delete_index_if_exists``):

      1. ``create_index(shadow_uid(base, now), primary_key="id")`` — the shadow is
         created WITH its primary key at creation (never inferred later), on a
         DETERMINISTIC epoch-embedded name so a crashed/retried run's orphan is
         reap-able by ``reap_stale_shadows`` from the name alone.
      2. ``_apply_settings(shadow)`` — the SAME helper ``ensure_index()`` uses, so
         the shadow's settings are provably identical to the live index's (zero
         drift by construction; asserted via ``settings_drift`` in tests).
      3. Stream every ``status == active`` item from Postgres (server-side, chunked)
         into the shadow with an EXPLICIT ``primary_key="id"``.
      4. Wait for ALL shadow-side Meili tasks (the settings updates AND every
         document batch) to reach ``succeeded`` — bounded by a generous TOTAL
         wall-clock budget. A failed task or a timeout raises.
      5. ``swap_indexes([(base, shadow)])`` (atomic) + wait for the swap task.
      6. ``delete_index_if_exists(shadow)`` — post-swap the shadow name holds the
         OLD data, so this reclaims it.

    **Failure safety:** any failure BEFORE the swap (settings error, a failed/timed
    -out document task) leaves the LIVE index completely untouched and best-effort
    deletes the partial shadow, then re-raises so the task's ``MEILI_RETRY`` policy
    can retry (a retry creates a NEW shadow at a fresh epoch; a stale partial is
    swept by ``reap_stale_shadows``). The live index only changes at the single
    atomic ``swap_indexes`` call.
    """
    import time

    from sqlalchemy import select

    from filearr.config import get_settings
    from filearr.db import SessionLocal
    from filearr.models import Item, ItemStatus, Library
    from filearr.search import (
        _apply_settings,
        build_doc,
        client,
        load_projection_defs,
        parent_scope_map,
    )

    s = get_settings()
    base = s.meili_index
    batch = s.meili_rebuild_batch
    total_budget = s.meili_rebuild_wait_s if wait_s is None else wait_s
    shadow_name = shadow_uid(base, datetime.now(UTC))
    total = 0

    async with client() as c:
        # Defensive: a within-the-same-second retry could reuse the name; drop any
        # leftover partial before (re)creating so create_index cannot collide.
        await c.delete_index_if_exists(shadow_name)
        shadow = await c.create_index(shadow_name, primary_key="id")
        task_uids: list[int] = []
        try:
            # Same settings routine ensure_index() uses (provably no drift).
            settings_tasks: list = []
            await _apply_settings(shadow, task_sink=settings_tasks)
            task_uids.extend(t.task_uid for t in settings_tasks)

            # P4-T6: facetable/sortable custom-field defs projected into every
            # shadow document (loaded once for the whole backfill).
            projection_defs = await load_projection_defs()

            async with SessionLocal() as session:
                # P3-T11: {library_id: expose_gps} for the GPS default-hidden gate,
                # loaded once so the rebuild honours each library's opt-in.
                expose_gps = {
                    lid: bool(eg)
                    for lid, eg in (
                        await session.execute(select(Library.id, Library.expose_gps))
                    ).all()
                }
                offset = 0
                while True:
                    items = (
                        (
                            await session.execute(
                                select(Item)
                                .where(Item.status == ItemStatus.active)
                                .order_by(Item.id)
                                .offset(offset)
                                .limit(batch)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if not items:
                        break
                    # P6-T3: parents' path_scope so sidecars inherit RBAC scope.
                    pscope = await parent_scope_map(session, items)
                    info = await shadow.update_documents(
                        [
                            build_doc(
                                i,
                                projection_defs,
                                expose_gps=expose_gps.get(i.library_id, False),
                                parent_path_scope=pscope.get(i.sidecar_of),
                            )
                            for i in items
                        ],
                        primary_key="id",
                    )
                    task_uids.append(info.task_uid)
                    total += len(items)
                    offset += batch

            # Wait for every shadow task to finish, bounding the TOTAL wall clock so
            # a stuck task cannot hang the worker forever. wait_for_task raises on
            # its own timeout; an exceeded total budget raises here.
            deadline = time.monotonic() + total_budget
            for uid in task_uids:
                remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    raise TimeoutError(
                        f"rebuild_via_swap: shadow {shadow_name!r} exceeded the "
                        f"{total_budget}s task-wait budget"
                    )
                res = await c.wait_for_task(uid, timeout_in_ms=remaining_ms)
                if res.status != "succeeded":
                    raise RuntimeError(
                        f"rebuild_via_swap: shadow task {uid} finished "
                        f"{res.status!r}, not 'succeeded' — aborting before swap"
                    )
        except BaseException:
            # PRE-SWAP failure: the live index was never touched. Best-effort drop
            # the partial shadow so it does not linger (the sweep is the backstop).
            with contextlib.suppress(Exception):
                await c.delete_index_if_exists(shadow_name)
            raise

        # The single atomic point where the live index changes.
        swap_info = await c.swap_indexes([(base, shadow_name)])
        swap_ms = int(max(1.0, (deadline - time.monotonic())) * 1000)
        await c.wait_for_task(swap_info.task_uid, timeout_in_ms=swap_ms)
        # Post-swap the shadow name holds the OLD data; reclaim it (best-effort:
        # the swap already succeeded, so a delete hiccup is non-fatal — the sweep
        # reaps it later).
        with contextlib.suppress(Exception):
            await c.delete_index_if_exists(shadow_name)

    logger.info("rebuild_via_swap: rebuilt %s via shadow %s (%d docs)", base, shadow_name, total)
    return total


async def reap_stale_shadows(
    *,
    now: datetime | int | float | None = None,
    max_age: timedelta | int | float | None = None,
) -> list[str]:
    """Delete orphaned shadow indexes left by a crashed/retried rebuild (P9-T5).

    Lists every Meili index and deletes those matching the ``shadow_uid`` naming
    scheme older than ``max_age`` (default ``FILEARR_MEILI_SHADOW_MAX_AGE_HOURS``,
    6h). A live rebuild's in-flight shadow is younger than the age bound and so is
    never reaped mid-build. Non-shadow indexes (the live index) never match
    ``is_stale_shadow`` and are untouched. Returns the reaped uids.
    """
    from filearr.config import get_settings
    from filearr.search import client

    s = get_settings()
    now = datetime.now(UTC) if now is None else now
    if max_age is None:
        max_age = timedelta(hours=s.meili_shadow_max_age_hours)

    reaped: list[str] = []
    async with client() as c:
        indexes = await c.get_indexes() or []
        for idx in indexes:
            uid = idx.uid
            if is_stale_shadow(uid, now, max_age):
                await c.delete_index_if_exists(uid)
                reaped.append(uid)
    if reaped:
        logger.info(
            "reap_stale_shadows: deleted %d orphan shadow index(es): %s", len(reaped), reaped
        )
    return reaped


async def ensure_webhook() -> None:
    """P9-T6: idempotently register the failure-observability webhook.

    Wiring: ``get_webhooks()`` → if the target URL is absent, ``create_webhook``
    with ``WebhookTarget.auth_headers()``.
    """
    raise NotImplementedError("P9-T6: ensure_webhook registration")
