"""Search endpoint — flat typed params translated to Meilisearch filter syntax,
opaque cursor pagination wrapping the engine's offset model."""

import base64
import json
import logging
import re

from fastapi import APIRouter, Depends, Query, Request
from meilisearch_python_sdk.errors import MeilisearchApiError
from meilisearch_python_sdk.models.search import Hybrid

from filearr.config import get_settings
from filearr.embed import embed_query
from filearr.file_groups import FILE_CATEGORIES, FILE_GROUPS
from filearr.meili_ops import DEFAULT_EMBEDDER_NAME
from filearr.schemas import SearchResponse
from filearr.search import client
from filearr.security import require_search_scope

router = APIRouter()

log = logging.getLogger("filearr.search")

PAGE_SIZE = 50


def _is_facet_unavailable(err: MeilisearchApiError) -> bool:
    """True when a Meili search failed only because a REQUESTED facet is not (yet)
    filterable on the index.

    This is the transient window after a new facet-able attribute is added to
    ``FILTERABLE_ATTRIBUTES``: ``ensure_index()`` applies the setting at boot, but
    Meilisearch re-indexes the whole corpus before the attribute becomes
    facetable, and on a large index that takes minutes. A facet request during
    that window must degrade (drop facet COUNTS) rather than 500 the entire search
    — losing counts briefly is fine; losing all results is a deploy blocker
    (regression found live 2026-07-18 when ``file_group`` shipped)."""
    code = getattr(err, "code", "") or ""
    return code == "invalid_search_facets" or "not filterable" in str(err).lower()

# Facets requested on every search. ``size``/``mtime`` are here only so Meili
# returns their ``facetStats`` (min/max) — the P3-T4 range sliders derive their
# bounds from those stats, never from hardcoded constants. Both are numeric and
# facet-search-disabled (meili_ops.FACET_SEARCH_DISABLED), so requesting them as
# facets is cheap: we consume the stats, not the (near-unique) value distribution.
FACETS = [
    "file_category",
    "file_group",
    "extension",
    "year",
    "tags",
    "status",
    "is_sidecar",
    "size",
    "mtime",
]

# P3-T5 highlighting/cropping. ``body_text`` (document body) is cropped to a short
# window around the match for the result snippet; ``title``/``filename`` get inline
# highlight markers. Meili wraps matches in the pre/post tags below; we pass the
# whole ``_formatted`` block through to the frontend as ``highlight`` (title/
# filename) + ``snippet`` (cropped body). The frontend renders these SAFELY (text
# nodes + <mark>, never {@html}) — the tags are the ONLY markup that ever appears.
HIGHLIGHT_ATTRS = ["body_text", "title", "filename"]
CROP_ATTRS = ["body_text"]
CROP_LENGTH = 30  # words of context around a body match (a snippet, not a page)
HIGHLIGHT_PRE = "<em>"
HIGHLIGHT_POST = "</em>"


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(cursor)).get("o", 0))
    except Exception:
        return 0


# P3-T1: a content hash is a lowercase-hex string of 8..64 chars (a truncated
# xxh3 quick_hash up to a full sha256/xxh128). Validated BEFORE the value is ever
# interpolated into a Meili filter string (defense in depth alongside the
# endpoint's ``Query(pattern=...)`` — no user input reaches a filter unchecked).
HASH_RE = re.compile(r"^[0-9a-f]{8,64}$")


def build_filters(
    *,
    file_category: list[str] | None = None,
    file_group: list[str] | None = None,
    library: str | None = None,
    status: str | None = "active",
    extension: str | None = None,
    year_gte: int | None = None,
    year_lte: int | None = None,
    size_gte: int | None = None,
    size_lte: int | None = None,
    mtime_gte: int | None = None,
    mtime_lte: int | None = None,
    tags: str | None = None,
    sidecar_of: str | None = None,
    include_sidecars: bool = False,
    hash: str | None = None,
) -> list[str]:
    """Build Meilisearch filter clauses. Sidecars are excluded by default (T3);
    an explicit ``sidecar_of`` or ``include_sidecars`` opts them back in. A
    ``hash`` adds an exact ``quick_hash``/``content_hash`` OR match (P3-T1). The
    ``size_*``/``mtime_*`` bounds (P3-T4) become numeric range filters — ``size``
    in bytes, ``mtime`` in epoch seconds (matching build_doc's projection)."""
    filters: list[str] = []
    if hash is not None:
        if not HASH_RE.match(hash):
            # Should be unreachable via the endpoint (Query validates first); a
            # hard guard so a direct caller cannot inject filter syntax.
            raise ValueError("hash must be 8-64 lowercase hex chars")
        # Exact match against either digest column; typo tolerance is disabled on
        # both (HASH_ATTRIBUTES), so a one-hex-digit difference matches nothing.
        filters.append(f"(quick_hash = '{hash}' OR content_hash = '{hash}')")
    if file_category:
        # W8-A: repeatable => OR (a file has exactly one category). Every value is
        # validated against the controlled ``FILE_CATEGORIES`` vocabulary BEFORE it
        # is interpolated (same injection-safe posture as ``file_group``/``hash``).
        # Unknown values are dropped.
        valid_cats = [c for c in file_category if c in FILE_CATEGORIES]
        if valid_cats:
            filters.append(
                "(" + " OR ".join(f"file_category = '{c}'" for c in valid_cats) + ")"
            )
    if file_group:
        # Repeatable => OR semantics (a file has exactly one group). Every value is
        # validated against the controlled ``FILE_GROUPS`` vocabulary BEFORE it is
        # interpolated, so no user input reaches the Meili filter unchecked (same
        # defense-in-depth posture as the hash filter). Unknown values are dropped.
        valid = [g for g in file_group if g in FILE_GROUPS]
        if valid:
            filters.append(
                "(" + " OR ".join(f"file_group = '{g}'" for g in valid) + ")"
            )
    if library:
        filters.append(f"library_id = '{library}'")
    if status:
        filters.append(f"status = '{status}'")
    if extension:
        filters.append(f"extension = '{extension}'")
    if year_gte is not None:
        filters.append(f"year >= {year_gte}")
    if year_lte is not None:
        filters.append(f"year <= {year_lte}")
    # P3-T4 numeric range filters. Values are ints (FastAPI-coerced), so the
    # interpolation is injection-safe; ``size`` is bytes, ``mtime`` epoch seconds.
    if size_gte is not None:
        filters.append(f"size >= {int(size_gte)}")
    if size_lte is not None:
        filters.append(f"size <= {int(size_lte)}")
    if mtime_gte is not None:
        filters.append(f"mtime >= {int(mtime_gte)}")
    if mtime_lte is not None:
        filters.append(f"mtime <= {int(mtime_lte)}")
    if tags:
        filters.extend(f"tags = '{t.strip()}'" for t in tags.split(","))
    if sidecar_of:
        # Explicitly requesting a parent's sidecars implies including sidecars.
        filters.append(f"sidecar_of = '{sidecar_of}'")
    elif not include_sidecars:
        # T3 default: hide sidecar files (episode .nfo/thumb, poster.jpg, ...).
        filters.append("is_sidecar = false")
    return filters


def _shape_hit(hit: dict) -> dict:
    """Attach the safe ``snippet`` (cropped body) + ``highlight`` (title/filename)
    to a search hit and drop the raw ``body_text`` / ``_formatted`` payload (P3-T5).

    Meili returns ``_formatted`` with matches wrapped in ``<em>``/``</em>``; we
    surface ONLY those cropped/highlighted strings (marker tags are the sole
    markup). The full ``body_text`` (up to the index cap) is removed from the hit
    so a search response never ships kilobytes of body per row — the frontend uses
    the short snippet, and renders every value as text + <mark>, never {@html}."""
    out = {k: v for k, v in hit.items() if k not in ("body_text", "_formatted")}
    fmt = hit.get("_formatted")
    if isinstance(fmt, dict):
        snippet = fmt.get("body_text")
        if isinstance(snippet, str) and snippet.strip():
            out["snippet"] = snippet
        highlight = {
            key: fmt[key]
            for key in ("title", "filename")
            if isinstance(fmt.get(key), str)
        }
        if highlight:
            out["highlight"] = highlight
    return out


@router.get("/search", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = "",
    file_category: list[str] | None = Query(
        default=None,
        description="file-category filter (repeatable = OR); the coarse, extension-"
        "derived parent of file_group (the authoritative type filter, successor to "
        "the removed media_type). See GET /taxonomy for the vocabulary. Unknown "
        "values are ignored.",
    ),
    file_group: list[str] | None = Query(
        default=None,
        description="file-group filter (repeatable = OR); the finer, extension-"
        "derived similarity bucket. See GET /system/file-groups for the vocabulary. "
        "Unknown values are ignored.",
    ),
    library: str | None = None,
    status: str | None = Query(default="active"),
    extension: str | None = None,
    year_gte: int | None = None,
    year_lte: int | None = None,
    size_gte: int | None = Query(
        default=None, ge=0, description="P3-T4 min file size in bytes (inclusive)"
    ),
    size_lte: int | None = Query(
        default=None, ge=0, description="P3-T4 max file size in bytes (inclusive)"
    ),
    mtime_gte: int | None = Query(
        default=None, description="P3-T4 min mtime as epoch seconds (inclusive)"
    ),
    mtime_lte: int | None = Query(
        default=None, description="P3-T4 max mtime as epoch seconds (inclusive)"
    ),
    tags: str | None = Query(default=None, description="comma-separated, AND semantics"),
    sidecar_of: str | None = Query(
        default=None, description="return sidecars linked to this parent item id"
    ),
    include_sidecars: bool = Query(
        default=False,
        description="include sidecar files (.nfo/artwork/JRiver) in results; "
        "excluded by default so they don't pollute top-level hits",
    ),
    hash: str | None = Query(
        default=None,
        pattern="^[0-9a-f]{8,64}$",
        description="P3-T1 exact hash lookup: matches quick_hash OR content_hash "
        "(lowercase hex, 8-64 chars). Typo tolerance is off for these fields, so "
        "a single differing digit returns nothing.",
    ),
    sort: str | None = Query(
        default=None,
        pattern="^(newest|(title|year|size|mtime):(asc|desc))$",
        description="explicit ordering; 'newest' is an alias for 'mtime:desc'. "
        "Distinct from the ambient recency tie-breaker (always on).",
    ),
    semantic: float = Query(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="P3-T8 hybrid semantic ratio (0..1). 0 = pure keyword "
        "(default, unchanged behaviour); 1 = pure vector; in between blends both. "
        "Ignored (treated as 0) unless semantic search is enabled server-side.",
    ),
    cursor: str | None = None,
    limit: int = Query(default=PAGE_SIZE, le=200),
    # P6-T3 server-side RBAC scoping: None (admin / API key / auth-off) => no
    # filter (byte-identical to the pre-P6 path); else the deny-aware scope
    # expression ANDed into the Meili query so Meili does the row-level filtering.
    scope_filter: str | None = Depends(require_search_scope("read")),
) -> SearchResponse:
    filters = build_filters(
        file_category=file_category,
        file_group=file_group,
        library=library,
        status=status,
        extension=extension,
        year_gte=year_gte,
        year_lte=year_lte,
        size_gte=size_gte,
        size_lte=size_lte,
        mtime_gte=mtime_gte,
        mtime_lte=mtime_lte,
        tags=tags,
        sidecar_of=sidecar_of,
        include_sidecars=include_sidecars,
        hash=hash,
    )
    # P6-T3: narrow to the caller's granted scopes (Meili-side enforcement).
    if scope_filter:
        filters.append(scope_filter)

    # 'newest' is a friendly alias. FIX-3: it maps to the CLAMPED ``mtime_sort``
    # (min(mtime, index-time)) so a file with a bogus FUTURE mtime cannot float to
    # the top of a "newest" sort. Raw ``mtime`` stays available for an explicit
    # ``sort=mtime:asc|desc`` (display/debugging) and for range filters.
    sort_expr = "mtime_sort:desc" if sort == "newest" else sort

    offset = _decode_cursor(cursor)
    s = get_settings()

    # P3-T8 hybrid search: when the caller asks for a semantic blend AND the
    # feature is enabled, embed the query locally (39 ms, approved) and hand Meili
    # a hybrid {semanticRatio, embedder} plus the query vector. When semantic is 0
    # or disabled we pass neither, so the keyword path is byte-identical to before.
    hybrid = None
    vector = None
    if semantic > 0.0 and s.semantic_enabled:
        vector = embed_query(q)
        hybrid = Hybrid(semantic_ratio=semantic, embedder=DEFAULT_EMBEDDER_NAME)

    search_kwargs = dict(
        filter=" AND ".join(filters) if filters else None,
        offset=offset,
        limit=limit,
        sort=[sort_expr] if sort_expr else None,
        hybrid=hybrid,
        vector=vector,
        # P3-T5 snippets/highlighting. body_text is cropped to a short window;
        # title/filename get inline markers. Custom <em>/</em> tags (also the
        # SDK default) are the only markup Meili injects.
        attributes_to_highlight=HIGHLIGHT_ATTRS,
        attributes_to_crop=CROP_ATTRS,
        crop_length=CROP_LENGTH,
        highlight_pre_tag=HIGHLIGHT_PRE,
        highlight_post_tag=HIGHLIGHT_POST,
    )
    async with client() as c:
        index = c.index(s.meili_index)
        try:
            result = await index.search(q, facets=FACETS, **search_kwargs)
        except MeilisearchApiError as err:
            # A newly-added facet still re-indexing must not take down search:
            # retry once WITHOUT facet distribution (results survive; counts drop
            # until the reindex completes). Any other API error is a real fault.
            if not _is_facet_unavailable(err):
                raise
            log.warning(
                "search facets unavailable (%s); serving without facet counts — a "
                "newly-added filterable attribute is likely still re-indexing",
                getattr(err, "code", "?"),
            )
            result = await index.search(q, facets=None, **search_kwargs)

    total = result.estimated_total_hits or 0
    next_cursor = _encode_cursor(offset + limit) if offset + limit < total else None
    # ``facet_stats`` (P3-T4): the SDK exposes per-numeric-facet min/max; pass it
    # through verbatim so the frontend range sliders derive bounds from real data.
    # A fake/older client that omits the attribute degrades to an empty dict.
    facet_stats = getattr(result, "facet_stats", None) or {}
    # P6-T9: opt-in read auditing (default OFF — read volume is high, value low
    # outside multi-tenant SaaS). Only fires when FILEARR_AUDIT_READS=true.
    if s.audit_reads:
        from filearr import audit

        await audit.emit(
            audit.SEARCH,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "q": q,
                "file_category": file_category,
                "library": library,
                "total": total,
            },
        )
    return SearchResponse(
        hits=[_shape_hit(h) for h in result.hits],
        total=total,
        facets=result.facet_distribution or {},
        facet_stats=facet_stats,
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# P3-T12 — tag facet type-ahead. A thin proxy over Meili's FACET-SEARCH endpoint
# (``AsyncIndex.facet_search``) against the existing ``tags`` array. Configuration
# -free: it inherits the index's typo tolerance (``tags`` is intentionally NOT in
# ``DISABLE_TYPO_ATTRIBUTES``, so a typo'd prefix still matches) and, per P9 R2,
# the facet VALUES come back COUNT-ordered because ``tags`` is a
# ``FACET_SEARCH_CANDIDATE`` with ``sortFacetValuesBy=count``. An optional
# ``type``/``library``/``status`` context narrows the suggestion counts to the
# current search scope (same ``build_filters`` shape, injection-safe).
# ---------------------------------------------------------------------------
@router.get("/search/tags")
async def search_tags(
    q: str = Query(default="", description="tag prefix/substring to complete"),
    file_category: list[str] | None = Query(
        default=None, description="scope suggestions to one or more file categories"
    ),
    library: str | None = None,
    status: str | None = Query(default="active"),
    limit: int = Query(default=20, ge=1, le=50),
    scope_filter: str | None = Depends(require_search_scope("read")),
) -> dict:
    """Typo-tolerant, count-ordered tag suggestions (P3-T12).

    Returns ``{"tags": [{"value": tag, "count": n}, ...]}`` — the most-common
    matching tags first (R2). ``q`` is the partial tag the user is typing; an
    empty ``q`` returns the top tags in the (optionally scoped) corpus."""
    # Optional scope context — build_filters excludes sidecars by default and adds
    # the equality clauses; the tag facet counts then reflect that scope.
    filters = build_filters(file_category=file_category, library=library, status=status)
    # P6-T3: apply the caller's RBAC scope filter to the facet counts too, so tag
    # suggestions never leak values that live only under un-granted paths.
    if scope_filter:
        filters.append(scope_filter)
    filter_expr = " AND ".join(filters) if filters else None

    s = get_settings()
    async with client() as c:
        res = await c.index(s.meili_index).facet_search(
            facet_name="tags",
            facet_query=q,
            filter=filter_expr,
        )

    # facet_hits carries {value, count}; the SDK already orders per the index
    # ``sortFacetValuesBy`` (count for tags). Cap to ``limit`` defensively.
    hits = getattr(res, "facet_hits", None) or []
    tags = [
        {"value": h.value, "count": h.count}
        for h in hits[:limit]
    ]
    return {"tags": tags}


# ---------------------------------------------------------------------------
# P3-T7 — the vocabulary of accepted ``/search`` query params, DERIVED from the
# endpoint signature (single source of truth) minus the opaque ``cursor`` (which
# is pagination state, never part of a saved query). ``saved_searches`` validates
# every stored ``params`` key against this frozenset, so:
#   * adding a new search param auto-extends the saved-search vocabulary (no
#     second edit), and
#   * renaming/removing a param makes the saved-search round-trip test fail loudly
#     rather than silently accepting a now-dead key.
# ``inspect.signature`` reads the ORIGINAL function (FastAPI's route decorator
# returns it unchanged), so this reflects exactly what a caller may pass.
# ---------------------------------------------------------------------------
import inspect  # noqa: E402  (kept next to its sole use)

SEARCH_PARAM_NAMES: frozenset[str] = frozenset(
    name
    for name in inspect.signature(search).parameters
    # ``cursor`` is pagination state; ``scope_filter`` is the P6-T3 injected RBAC
    # dependency (Depends), never a user-supplied search param.
    if name not in ("cursor", "scope_filter")
)
