"""Meilisearch projection layer.

The index is DERIVED and rebuildable from Postgres at any time — never treat it
as a store of record. Uses meilisearch-python-sdk (async-first)."""

import logging
from datetime import UTC, datetime
from typing import Any

from meilisearch_python_sdk import AsyncClient
from meilisearch_python_sdk.models.settings import (
    Faceting,
    Filter,
    FilterableAttributeFeatures,
    FilterableAttributes,
    TypoTolerance,
)

from filearr.config import get_settings
from filearr.custom_fields import (
    CustomFieldDef,
    cf_meili_attribute,
    def_from_model,
    project_custom_fields_to_meili,
)
from filearr.embed import EMBEDDING_KEY, has_current_embedding
from filearr.exif import strip_gps
from filearr.meili_ops import (
    DEFAULT_EMBEDDER_NAME,
    DISABLE_TYPO_ATTRIBUTES,
    FACET_SEARCH_CANDIDATES,
    FACET_SEARCH_DISABLED,
    FILTERABLE_ATTRIBUTES,
    RANKING_RULES,
    SEARCHABLE_ATTRIBUTES,
    SORTABLE_ATTRIBUTES,
    build_embedders,
    embedder_matches,
    settings_drift,
)
from filearr.models import Item
from filearr.tenant_tokens import scope_ancestors

logger = logging.getLogger(__name__)

# Single source of truth for the attribute lists is ``filearr.meili_ops`` (P9-T1);
# these module-level names are kept as back-compat aliases (older imports / tests
# referenced them here). Do NOT re-hardcode — always mirror meili_ops.
SEARCHABLE = list(SEARCHABLE_ATTRIBUTES)
FILTERABLE = list(FILTERABLE_ATTRIBUTES)
SORTABLE = list(SORTABLE_ATTRIBUTES)

# Meili's own default (100); pinned explicitly so a ``GET /settings`` drift check
# never reports phantom drift on the faceting sub-object just because we omitted it.
MAX_VALUES_PER_FACET = 100


def client() -> AsyncClient:
    s = get_settings()
    return AsyncClient(s.meili_url, s.meili_master_key)


def _facet_value_order() -> dict[str, str]:
    """``sortFacetValuesBy`` map (R2): COUNT-order the genuine facet candidates so
    the most-common value surfaces first (the P3-T12 tag type-ahead consumer),
    ``alpha`` (Meili default) for everything else via the ``*`` wildcard."""
    order: dict[str, str] = {"*": "alpha"}
    order.update({attr: "count" for attr in FACET_SEARCH_CANDIDATES})
    return order


def _filterable_settings(
    cf_filterable: list[str] | None = None,
) -> list[FilterableAttributes]:
    """Object-form filterable attributes (P9-T1): disable facet search on the
    high-cardinality numeric fields (``size``/``mtime``/``year``) while leaving
    equality + comparison filtering intact for every attribute. Typed SDK models
    (``FilterableAttributes``/``FilterableAttributeFeatures``/``Filter``), never
    dicts (CLAUDE.md gotcha).

    P4-T6: ``cf_filterable`` (the ``cf_<name>`` attributes of the currently
    ``facetable`` custom fields, computed from the ``custom_fields`` table at
    apply time) is appended STATIC + dynamic. Facet search stays enabled on them
    (they are not in ``FACET_SEARCH_DISABLED``). This settings change only ever
    lands via the rebuild-and-swap path — never an in-place update call."""
    out: list[FilterableAttributes] = []
    for attr in [*FILTERABLE_ATTRIBUTES, *(cf_filterable or [])]:
        out.append(
            FilterableAttributes(
                attribute_patterns=[attr],
                features=FilterableAttributeFeatures(
                    facet_search=attr not in FACET_SEARCH_DISABLED,
                    filter=Filter(equality=True, comparison=True),
                ),
            )
        )
    return out


def _typo_tolerance() -> TypoTolerance:
    """Typed ``typoTolerance`` (P9-T1): disable fuzzy matching on the structural /
    numeric / hash-shaped fields (``meili_ops.DISABLE_TYPO_ATTRIBUTES`` — derived
    from the ``hashx.HASH_ATTRIBUTES`` single source ∪ ``year``/``size``)."""
    return TypoTolerance(enabled=True, disable_on_attributes=list(DISABLE_TYPO_ATTRIBUTES))


def _faceting() -> Faceting:
    return Faceting(
        max_values_per_facet=MAX_VALUES_PER_FACET,
        sort_facet_values_by=_facet_value_order(),
    )


def _desired_settings(
    cf_filterable: list[str] | None = None,
    cf_sortable: list[str] | None = None,
) -> dict[str, Any]:
    """The managed settings, in the camelCase shape a *projected* ``get_settings()``
    is compared against by ``meili_ops.settings_drift`` (drift-skip idempotency).
    The per-attribute facet-search flag is folded into a hashable ``"attr:bool"``
    token so ``filterableAttributes`` compares set-wise without unhashable dicts.

    P4-T6: the STATIC attribute lists are extended with the dynamic ``cf_<name>``
    filterable/sortable attributes derived from the ``custom_fields`` table so a
    ``facetable``/``sortable`` toggle shows up as genuine settings drift and is
    (re)applied on the next rebuild-and-swap."""
    cf_filterable = cf_filterable or []
    cf_sortable = cf_sortable or []
    return {
        "searchableAttributes": list(SEARCHABLE_ATTRIBUTES),
        "filterableAttributes": [
            f"{attr}:{attr not in FACET_SEARCH_DISABLED}"
            for attr in [*FILTERABLE_ATTRIBUTES, *cf_filterable]
        ],
        "sortableAttributes": [*SORTABLE_ATTRIBUTES, *cf_sortable],
        "rankingRules": list(RANKING_RULES),
        "typoTolerance": {
            "enabled": True,
            "disableOnAttributes": list(DISABLE_TYPO_ATTRIBUTES),
        },
        "faceting": {
            "maxValuesPerFacet": MAX_VALUES_PER_FACET,
            "sortFacetValuesBy": _facet_value_order(),
        },
        "searchCutoffMs": get_settings().meili_search_cutoff_ms,
    }


def _project_current(cur: Any) -> dict[str, Any]:
    """Project a live ``MeilisearchSettings`` down to EXACTLY the managed keys, in
    the same shape as ``_desired_settings`` so ``settings_drift`` compares like for
    like. Meili's default extra typo sub-keys (``disableOnWords`` /
    ``minWordSizeForTypos`` / ``disableOnNumbers``) are intentionally dropped so a
    nested full-dict comparison does not report phantom drift on them.

    NOTE (live-verify, P9 open question): assumes Meili echoes object-form
    filterable attributes and the ``sortFacetValuesBy`` map back on ``GET /settings``
    as set. If a live instance normalises these differently the only cost is an
    (idempotent, harmless) settings re-apply on each boot — never incorrectness."""
    filt: list[str] = []
    for el in cur.filterable_attributes or []:
        if isinstance(el, str):
            filt.append(f"{el}:True")
        else:
            for pat in el.attribute_patterns:
                filt.append(f"{pat}:{el.features.facet_search}")
    tt = cur.typo_tolerance
    fac = cur.faceting
    return {
        "searchableAttributes": list(cur.searchable_attributes or []),
        "filterableAttributes": filt,
        "sortableAttributes": list(cur.sortable_attributes or []),
        "rankingRules": list(cur.ranking_rules or []),
        "typoTolerance": {
            "enabled": tt.enabled if tt else True,
            "disableOnAttributes": list(tt.disable_on_attributes or []) if tt else [],
        },
        "faceting": {
            "maxValuesPerFacet": fac.max_values_per_facet if fac else None,
            "sortFacetValuesBy": dict(fac.sort_facet_values_by or {}) if fac else {},
        },
        "searchCutoffMs": cur.search_cutoff_ms,
    }


async def _cf_index_attributes() -> tuple[list[str], list[str]]:
    """Load the dynamic ``cf_<name>`` filterable (facetable) + sortable custom-field
    attributes from the ``custom_fields`` table for the Meili settings (P4-T6).

    Best-effort: any failure (DB unreachable at boot, table absent) degrades to
    the STATIC attribute set — the next rebuild/reconcile repairs it — rather than
    failing ``ensure_index``. Sorted for a deterministic settings-drift compare."""
    try:
        from sqlalchemy import or_, select

        from filearr.db import SessionLocal
        from filearr.models import CustomField

        async with SessionLocal() as session:
            rows = (
                await session.execute(
                    select(
                        CustomField.name, CustomField.facetable, CustomField.sortable
                    ).where(
                        or_(
                            CustomField.facetable.is_(True),
                            CustomField.sortable.is_(True),
                        )
                    )
                )
            ).all()
    except Exception:
        logger.warning(
            "cf index attributes: could not load custom_fields; "
            "falling back to static attribute set",
            exc_info=True,
        )
        return [], []
    filt = sorted(cf_meili_attribute(name) for name, fac, _srt in rows if fac)
    sort = sorted(cf_meili_attribute(name) for name, _fac, srt in rows if srt)
    return filt, sort


async def load_projection_defs() -> list[CustomFieldDef]:
    """Load the ``facetable``/``sortable`` custom-field definitions ``build_doc``
    projects into a search document (P4-T6). Best-effort like
    :func:`_cf_index_attributes`: an unreachable DB yields ``[]`` (the doc simply
    omits ``cf_*`` attributes; the next reconcile/rebuild backfills them)."""
    try:
        from sqlalchemy import or_, select

        from filearr.db import SessionLocal
        from filearr.models import CustomField

        async with SessionLocal() as session:
            rows = (
                (
                    await session.execute(
                        select(CustomField).where(
                            or_(
                                CustomField.facetable.is_(True),
                                CustomField.sortable.is_(True),
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [def_from_model(r) for r in rows]
    except Exception:
        logger.warning(
            "projection defs: could not load custom_fields; "
            "documents will omit cf_* attributes",
            exc_info=True,
        )
        return []


async def _apply_embedder_settings(
    index: Any, *, task_sink: list[Any] | None = None
) -> None:
    """P3-T8: ensure the ``userProvided`` ``default`` embedder exists on ``index``
    when semantic search is enabled — idempotently and INDEPENDENTLY of the
    settings-drift gate (so a freshly-enabled install applies it even when the
    keyword settings already match). No-op when semantic search is disabled, so a
    default install never carries an embedder in its Meili settings (drift-safe).

    Vectors ride each document's ``_vectors`` block (userProvided source); this
    only registers the embedder's identity + dimensions so Meili stores and ANN-
    indexes them. ``task_sink`` (shadow-rebuild path) collects the settings task so
    the caller can wait for it before swapping."""
    s = get_settings()
    if not s.semantic_enabled:
        return
    current = await index.get_embedders()
    if embedder_matches(current, s.embed_dim):
        return
    info = await index.update_embedders(build_embedders(s.embed_dim))
    if task_sink is not None:
        task_sink.append(info)


async def _apply_settings(
    index: Any,
    *,
    task_sink: list[Any] | None = None,
    cf_filterable: list[str] | None = None,
    cf_sortable: list[str] | None = None,
) -> list[str]:
    """Apply the phase-9 target settings to ``index`` idempotently.

    THE single spec-driven settings helper (P9-T1/T2). ``ensure_index()`` calls it
    for the live index; P9-T5's shadow-index rebuild calls it for the shadow so
    both carry provably identical settings (verified by ``settings_drift``). Returns
    the drifted setting keys that were (re)applied — empty when the index already
    matches, so a second boot performs no settings work (idempotent).

    ``task_sink`` (P9-T5): when provided, the ``TaskInfo`` objects returned by each
    Meili settings-update call are appended to it, so the shadow-rebuild path can
    wait for those async settings tasks to finish before swapping the shadow into
    place. ``ensure_index()`` omits it (fire-and-forget at boot is fine).

    P4-T6: ``cf_filterable``/``cf_sortable`` are the dynamic ``cf_<name>``
    attributes from the ``custom_fields`` table. When BOTH are omitted they are
    loaded here via :func:`_cf_index_attributes` (so ``ensure_index`` and the
    shadow rebuild both pick up the current custom-field facet/sort config); pass
    explicit lists to bypass the load (tests / callers that already have them)."""
    if cf_filterable is None and cf_sortable is None:
        cf_filterable, cf_sortable = await _cf_index_attributes()
    cf_filterable = cf_filterable or []
    cf_sortable = cf_sortable or []
    # P3-T8: embedder settings are applied independently of the keyword-settings
    # drift gate below (a freshly-enabled semantic install must register the
    # embedder even when every other setting already matches).
    await _apply_embedder_settings(index, task_sink=task_sink)
    current = _project_current(await index.get_settings())
    drift = settings_drift(current, _desired_settings(cf_filterable, cf_sortable))
    if not drift:
        return []
    infos = [
        await index.update_searchable_attributes(list(SEARCHABLE_ATTRIBUTES)),
        await index.update_filterable_attributes(_filterable_settings(cf_filterable)),
        await index.update_sortable_attributes([*SORTABLE_ATTRIBUTES, *cf_sortable]),
        # P3-T2 recency: custom ranking rule (recency_bucket:asc) after exactness
        await index.update_ranking_rules(list(RANKING_RULES)),
        # numeric/hash/structural fields must not fuzzy-match (SDK typed model)
        await index.update_typo_tolerance(_typo_tolerance()),
        # facet-value ordering: count for the type-ahead candidates (R2)
        await index.update_faceting(_faceting()),
        # crafted-query DoS circuit-breaker (P9-T2): bound worst-case search time
        await index.update_search_cutoff_ms(get_settings().meili_search_cutoff_ms),
    ]
    if task_sink is not None:
        task_sink.extend(infos)
    return drift


async def ensure_index() -> None:
    s = get_settings()
    async with client() as c:
        index = await c.get_or_create_index(s.meili_index, primary_key="id")
        # FIX-2: get_or_create_index only sets primary_key when it CREATES the
        # index. If the index already existed (e.g. an implicit index created by
        # some earlier document push, as happened after the 1.49 volume wipe) its
        # primaryKey may be null or, worse, wrong. Enforce it here so every
        # projection has a stable "id" primary key.
        #   * None  -> set it now (Meili allows this while the index is empty;
        #              ensure_index runs at boot before any sync task fires).
        #   * "id"  -> already correct, nothing to do.
        #   * other -> refuse loudly rather than silently mis-projecting onto an
        #              index keyed on the wrong field.
        if index.primary_key is None:
            await index.update(primary_key="id")
        elif index.primary_key != "id":
            raise RuntimeError(
                f"Meili index {s.meili_index!r} has primary_key "
                f"{index.primary_key!r}, expected 'id'. Refusing to project onto a "
                "mismatched index -- drop and rebuild it (rebuild_index) first."
            )
        await _apply_settings(index)


# P3-T2 recency buckets. Coarse, monotonic-with-age integer buckets over the
# days-since-mtime axis. Deliberately COARSE (five buckets, not raw days/epoch) so
# the ``recency_bucket:asc`` custom ranking rule only breaks ties among otherwise
# equally-relevant hits instead of dominating relevance. Bucket boundaries (upper-
# exclusive, in days): 0 = [0, 7), 1 = [7, 30), 2 = [30, 180), 3 = [180, 365),
# 4 = [365, inf) AND unknown/missing mtime. A future-dated mtime (negative age)
# lands in bucket 0 (treated as "just touched"). Meili-only + disposable: derived
# in the projection, never persisted to Postgres (invariant 1).
_RECENCY_BUCKET_DAYS: tuple[int, ...] = (7, 30, 180, 365)
RECENCY_OLDEST_BUCKET: int = len(_RECENCY_BUCKET_DAYS)  # == 4

# FIX-3: how far into the future an mtime may sit before it is treated as SUSPECT
# (a bogus timestamp from a bad copy tool / mis-set camera clock) rather than as
# "just touched". A small window absorbs genuine clock skew between the scanner
# host and the file's source filesystem; anything past it buckets OLDEST so it
# can never dominate a recency tie-break or a "newest" sort.
_FUTURE_SKEW_SECONDS: int = 48 * 3600


def recency_bucket(mtime: datetime | None, now: datetime | None = None) -> int:
    """Bucket ``mtime`` into a coarse recency band (0 = newest ... 4 = oldest).

    ``None`` mtime (or one with no usable timestamp) falls in the oldest bucket so
    an undated item never spuriously wins a recency tie-break. Pure and
    now-injectable so boundary cases are deterministically testable.

    FIX-3 (future-mtime bug): a timestamp more than ``_FUTURE_SKEW_SECONDS`` (48h)
    in the future is SUSPECT — it lands in the OLDEST bucket (4), NOT bucket 0.
    Previously any future date bucketed 0 ("just touched"), so files with bogus
    future mtimes floated to the top of recency-ranked results. A future date
    WITHIN the 48h skew window still counts as freshly-touched (bucket 0)."""
    if mtime is None:
        return RECENCY_OLDEST_BUCKET
    now = now or datetime.now(UTC)
    if (mtime - now).total_seconds() > _FUTURE_SKEW_SECONDS:
        # Bogus future timestamp — treat as suspect, never as "newest".
        return RECENCY_OLDEST_BUCKET
    days = (now - mtime).days
    for bucket, upper in enumerate(_RECENCY_BUCKET_DAYS):
        if days < upper:
            return bucket
    return RECENCY_OLDEST_BUCKET


def _index_body_text(value: Any) -> str | None:
    """Cap the stored ``body_text`` down to the Meili INDEX ceiling (P3-T5).

    ``metadata_.body_text`` is stored up to ``body_text_max_chars`` (100k) in
    Postgres; only the first ``body_text_index_chars`` (20k) are projected into
    the searchable Meili attribute — index-bloat control (the prefix carries the
    overwhelming majority of useful matches). Non-string / empty → omit."""
    if not isinstance(value, str) or not value:
        return None
    cap = get_settings().body_text_index_chars
    return value[:cap] if len(value) > cap else value


def _combined_body(meta: dict[str, Any]) -> str | None:
    """Join native ``body_text`` (P3-T5) and OCR ``ocr_text`` (P3-T6) into the one
    searchable body field. OCR text is searchable via the SAME attribute and the
    SAME index cap as native document text — a scanned PDF/image and a text PDF hit
    the same snippet/highlight path. Either source may be absent."""
    parts = [
        v for v in (meta.get("body_text"), meta.get("ocr_text"))
        if isinstance(v, str) and v
    ]
    if not parts:
        return None
    return "\n".join(parts)


def _project_exif(meta: dict[str, Any], expose_gps: bool) -> dict[str, Any]:
    """Project the ``exif.*`` namespaced facts (P3-T11) into the search document,
    GATING GPS behind the per-library ``expose_gps`` flag.

    GPS keys live RAW in ``metadata_`` (extracted truth) but are stripped from the
    projection unless the library opted in — the CWE-1230 default-hidden gate (R5).
    ``strip_gps`` also matches the ``exif.gps_*`` namespaced keys (its last-segment
    rule), so an unexposed library never ships a GPS coordinate into Meili (or,
    therefore, into a ``/search`` hit or a facet)."""
    exif = {k: v for k, v in meta.items() if k.startswith("exif.")}
    if not exif:
        return {}
    if not expose_gps:
        exif = strip_gps(exif)
    return exif
def _index_archive_members(value: Any) -> str | None:
    """Cap the stored ``archive_members`` string down to the Meili INDEX ceiling
    (P3-T13). ``metadata_.archive_members`` is already stored capped, but the
    projection re-applies ``archive_members_index_chars`` defensively so a rebuild
    from an older, larger stored value still respects the index-bloat bound.
    Non-string / empty -> omit."""
    if not isinstance(value, str) or not value:
        return None
    cap = get_settings().archive_members_index_chars
    return value[:cap] if len(value) > cap else value


def build_doc(
    item: Item,
    custom_defs: list[CustomFieldDef] | None = None,
    *,
    expose_gps: bool = False,
    parent_path_scope: str | None = None,
) -> dict[str, Any]:
    """Flatten an Item (with user_metadata overlaid) into a search document.

    ``custom_defs`` (P4-T6): the ``facetable``/``sortable`` custom-field
    definitions to project. Each such field's effective value is emitted under
    ``cf_<name>`` (``date`` -> epoch int for stable facet typing); non-facetable /
    non-sortable fields are NOT projected (still visible via the Raw tab / PATCH
    response). Callers load the defs ONCE per batch (``load_projection_defs``) and
    pass them in — build_doc never touches the DB. The hardcoded top-level fields
    below (``artist``/``album``/``author``/``codec``/``resolution``/``genre``) are
    the profiles' ``facetable``/``sortable`` flags made concrete: those flags in
    ``filearr.profiles.METADATA_PROFILES`` are the source of truth for which lines
    exist here.

    **P10-T11/T12 note:** the item's network-open location (``share_url`` /
    ``share_source``) is deliberately NOT projected here. Share hints and central
    ``agent_share_maps`` mappings change without any item mutation, so baking a
    resolved URL into this disposable doc would go stale and force a reindex on
    every share-map edit; it is resolved at display time in
    ``api.items.get_item`` (``filearr.share_resolution``) instead."""
    meta = {**item.metadata_, **item.user_metadata}
    mtime_epoch = int(item.mtime.timestamp()) if item.mtime else None
    # FIX-3: clamp the SORT key to "now" so a bogus FUTURE mtime cannot float to
    # the top of a sort=newest (which uses mtime_sort:desc). The RAW ``mtime`` is
    # left untouched for display and range filters; only the derived sort key is
    # clamped. Computed at indexing time (now), so re-projecting an old doc via
    # rebuild-index re-clamps against the then-current clock.
    now_epoch = int(datetime.now(UTC).timestamp())
    doc = {
        "id": str(item.id),
        "library_id": str(item.library_id),
        "media_type": item.media_type.value,
        "status": item.status.value,
        "path": item.path,
        "rel_path": item.rel_path,
        "filename": item.filename,
        "extension": item.extension,
        "size": item.size,
        "mtime": mtime_epoch,
        # FIX-3 clamped sort key: min(mtime, index-time-now); None-safe.
        "mtime_sort": min(mtime_epoch, now_epoch) if mtime_epoch is not None else None,
        # P3-T2 recency tie-breaker bucket (Meili-only, disposable).
        "recency_bucket": recency_bucket(item.mtime),
        # P3-T1 exact-hash search targets (scan-time xxh3 digests).
        "quick_hash": item.quick_hash,
        "content_hash": item.content_hash,
        "title": item.title or meta.get("title") or item.filename,
        "year": item.year or meta.get("year"),
        "tags": item.tags,
        "artist": meta.get("artist"),
        "album": meta.get("album"),
        "author": meta.get("author"),
        "codec": meta.get("video_codec") or meta.get("audio_codec"),
        "resolution": meta.get("resolution"),
        "genre": meta.get("genre"),
        # P3-T5 searchable document body text (index-capped; snippets/highlighting).
        # P3-T5 native body text + P3-T6 OCR text (same searchable field/caps).
        "body_text": _index_body_text(_combined_body(meta)),
        # P3-T13 searchable archive member names (index-capped; "which archive
        # CONTAINS a file named X"). Meili-searchable only (LAST attribute).
        "archive_members": _index_archive_members(meta.get("archive_members")),
        "is_sidecar": item.sidecar_of is not None,
        "sidecar_of": str(item.sidecar_of) if item.sidecar_of else None,
        # P6-T3 RBAC scope key: the ARRAY of every ltree ancestor covering this
        # item, so a grant-scope equality test on the array matches by prefix. A
        # sidecar inherits its PARENT's scope when the caller supplies it
        # (``parent_path_scope`` — sidecars carry the parent's visibility, T3
        # ``sidecar_of``); otherwise it falls back to its own scan-time scope,
        # which for a real filesystem sidecar shares the parent's directory
        # ancestors anyway (directory-prefix grants inherit either way). An item
        # with no ``path_scope`` yet (pre-P6 rows, not re-scanned) projects ``[]``
        # → invisible to scope-filtered principals (fail-closed) but unaffected
        # for admin / auth-off (which inject no filter).
        "path_scope": list(
            scope_ancestors(
                (parent_path_scope if item.sidecar_of and parent_path_scope else item.path_scope)
                or ""
            )
        ),
    }
    if custom_defs:
        # P4-T6: project facetable/sortable custom fields under cf_<name>. Only
        # emitted when a value exists; the FILTERABLE/SORTABLE settings side is
        # handled at ensure_index/rebuild time and only via rebuild-and-swap.
        doc.update(project_custom_fields_to_meili(item.effective_metadata, custom_defs))
    # P3-T11: exif.* facts, GPS gated by the library's expose_gps flag (default
    # false => GPS absent from the projection, facets, and /search hits).
    doc.update(_project_exif(meta, expose_gps))
    # P3-T8: attach the semantic vector under _vectors ONLY when semantic search is
    # enabled AND the item carries a fingerprint-matching embedding. A drifted
    # (old-model) or missing vector is silently omitted — never mixed with current
    # ones (the /stats semantic section counts the mismatches for observability).
    if get_settings().semantic_enabled:
        cfg = get_settings().embedder_config
        if has_current_embedding(meta, cfg):
            doc["_vectors"] = {DEFAULT_EMBEDDER_NAME: meta[EMBEDDING_KEY]}
    return doc


async def parent_scope_map(session: Any, items: list[Item]) -> dict[Any, str | None]:
    """``{parent_item_id: path_scope}`` for the sidecars in ``items`` (P6-T3).

    Sidecars inherit their PARENT's RBAC scope (T3 ``sidecar_of``); this
    batch-loads the parents' ``path_scope`` so ``build_doc`` can project it. A
    parent already present in ``items`` is reused; the rest are fetched in one
    query. A missing / NULL parent scope simply falls back to the sidecar's own
    scope inside ``build_doc``. Returns ``{}`` when the batch has no sidecars."""
    from sqlalchemy import select

    have: dict[Any, str | None] = {i.id: i.path_scope for i in items}
    need = {i.sidecar_of for i in items if i.sidecar_of and i.sidecar_of not in have}
    if need:
        rows = (
            await session.execute(select(Item.id, Item.path_scope).where(Item.id.in_(need)))
        ).all()
        for pid, ps in rows:
            have[pid] = ps
    return have


async def upsert_docs(docs: list[dict[str, Any]]) -> None:
    s = get_settings()
    async with client() as c:
        # FIX-2: pass primary_key="id" explicitly. If the index somehow lacks a
        # primary key (unhealthy state ensure_index would normally have fixed),
        # this lets Meili infer "id" from the payload instead of guessing/erroring.
        # Ignored by the server once a primary key is already set.
        await c.index(s.meili_index).update_documents(docs, primary_key="id")


async def delete_docs(ids: list[str]) -> None:
    s = get_settings()
    async with client() as c:
        await c.index(s.meili_index).delete_documents(ids)
