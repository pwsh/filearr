"""Runtime File Extension Similarity Taxonomy service (W8-A).

The DB-backed, editable successor to ``media_types.detect``. It reads the
``file_categories`` / ``file_groups`` / ``file_group_extensions`` tables (seeded
from :mod:`filearr.file_groups`) into a **version-keyed in-process cache** and
classifies a path into a ``(file_category, file_group)`` pair at scan / extract /
replication time. Operators edit the taxonomy through the CRUD API
(:mod:`filearr.api.taxonomy`); every edit bumps ``taxonomy_state.version``, which
invalidates the cache on the next read.

SEED vs RUNTIME (the load-bearing split — also documented in ``file_groups.py``):

* **SEED** — :func:`filearr.file_groups.detect_group` / ``detect_category`` are the
  pure, session-free classifiers used by the SEARCH PROJECTION (``search.build_doc``
  has no DB session) and as this service's FALLBACK when the DB is empty/unreachable
  (e.g. at boot before the migration has seeded, or on a ``create_all`` test DB with
  no seed). They document the DEFAULT taxonomy.
* **RUNTIME** — this module. The stored ``items.file_category`` / ``items.file_group``
  columns are written from HERE, so operator edits take effect on the next scan.

Async-safety: the cache is a process-global snapshot guarded by an ``asyncio.Lock``
around the (rare) reload. A snapshot is immutable; ``detect`` on it is pure dict
lookups (zero DB I/O after the version matches), so a scan can classify thousands
of files against a single ``load()``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import file_groups as fg
from filearr.models import (
    FileCategoryModel,
    FileGroupExtension,
    FileGroupModel,
    TaxonomyState,
)

logger = logging.getLogger(__name__)

#: The sentinel version of the SEED fallback snapshot. Real DB versions start at 1
#: (the migration seeds ``taxonomy_state.version = 1``), so 0 never collides and a
#: fallback snapshot is always superseded once the DB is reachable + seeded.
_SEED_VERSION = 0

GROUP_OTHER = fg.GROUP_OTHER
CATEGORY_OTHER = fg.GROUP_OTHER  # the catch-all category key is also "other"


@dataclass(frozen=True)
class Taxonomy:
    """An immutable taxonomy snapshot (one ``taxonomy_state.version``)."""

    version: int
    #: ordered category dicts {key,label,description,extractor,sort_order,is_builtin}
    categories: tuple[dict, ...]
    #: ordered group dicts {key,label,description,category,sort_order,is_builtin,extensions}
    groups: tuple[dict, ...]
    ext_to_group: dict[str, str] = field(default_factory=dict)
    group_to_category: dict[str, str] = field(default_factory=dict)
    category_extractors: dict[str, str | None] = field(default_factory=dict)

    def detect(self, path: str) -> tuple[str, str]:
        """Classify ``path`` into ``(category_key, group_key)`` (pure; unknown ->
        ``("other", "other")``).

        Mirrors :func:`filearr.file_groups.detect_group`: case-insensitive,
        extension-only, a recognised compound ending (``.tar.gz`` …) consulted first
        and winning as a whole, a name with no usable extension (``.bashrc`` /
        ``README``) resolving to ``other``. The compound rule targets the ``archive``
        group when that group still exists in the (editable) taxonomy."""
        p = PurePath(path)
        suffixes = [s.lstrip(".").lower() for s in p.suffixes]
        if len(suffixes) >= 2:
            compound = f"{suffixes[-2]}.{suffixes[-1]}"
            gid = fg._COMPOUND_GROUP_MAP.get(compound)
            if gid is not None and gid in self.group_to_category:
                return self.group_to_category[gid], gid
        ext = p.suffix.lstrip(".").lower()
        gid = self.ext_to_group.get(ext)
        if gid is None:
            return CATEGORY_OTHER, GROUP_OTHER
        return self.group_to_category.get(gid, CATEGORY_OTHER), gid

    def extractor_for(self, category_key: str) -> str | None:
        """The extraction pipeline the category routes to (W8-B), or ``None`` for an
        unknown category / a category with no extractor."""
        return self.category_extractors.get(category_key)

    def primary_category_keys(self) -> list[str]:
        """Category keys that count as a "primary" (media-ish) sidecar-association
        parent (W8-E) — the categories that carry a non-null ``extractor``. In the
        default taxonomy that is exactly ``image`` / ``audio`` / ``video`` /
        ``document`` / ``three-d-cad`` (the buckets a subtitle / artwork / ``.nfo``
        sidecar can attach to); ``development`` / ``archive`` / ``system`` /
        ``other`` are never sidecar parents. Defining "primary" AS "has an
        extractor" keeps the agent's sidecar rule in lockstep with any operator
        taxonomy edit — add an extractor to a category and it becomes eligible."""
        return [c["key"] for c in self.categories if c["extractor"]]

    def agent_payload(self) -> dict:
        """The COMPACT, resolution-optimised taxonomy the distributed agent consumes
        (W8-E) — NOT the admin ``as_tree`` shape. Flat lookup maps keyed by the
        current ``version`` so an offline agent can classify a path into
        ``(file_category, file_group)``, route extraction, and decide sidecar-parent
        eligibility with zero further round-trips. Version-gated on the agent side,
        so shipping the full ~1271-entry ``ext_to_group`` map per fetch is fine.

        Shape (FROZEN — the Go ``internal/taxonomy`` package parses it)::

            {"version": N,
             "ext_to_group":       {ext: group_key, ...},
             "group_to_category":  {group_key: category_key, ...},
             "category_extractor": {category_key: extractor|null, ...},
             "primary_categories": [category_key, ...]}
        """
        return {
            "version": self.version,
            "ext_to_group": dict(self.ext_to_group),
            "group_to_category": dict(self.group_to_category),
            "category_extractor": dict(self.category_extractors),
            "primary_categories": self.primary_category_keys(),
        }

    def as_tree(self) -> dict:
        """The full nested tree the ``GET /api/v1/taxonomy`` endpoint returns —
        FROZEN shape (W8-C builds against it)::

            {"version": N,
             "tree": [{"category": {key,label,description,extractor,sort_order,
                                    is_builtin},
                       "groups": [{key,label,description,sort_order,is_builtin,
                                   extensions:[...]}]}]}
        """
        by_cat: dict[str, list[dict]] = {}
        for g in self.groups:
            by_cat.setdefault(g["category"], []).append(
                {
                    "key": g["key"],
                    "label": g["label"],
                    "description": g["description"],
                    "sort_order": g["sort_order"],
                    "is_builtin": g["is_builtin"],
                    "extensions": list(g["extensions"]),
                }
            )
        tree = [
            {
                "category": {
                    "key": c["key"],
                    "label": c["label"],
                    "description": c["description"],
                    "extractor": c["extractor"],
                    "sort_order": c["sort_order"],
                    "is_builtin": c["is_builtin"],
                },
                "groups": by_cat.get(c["key"], []),
            }
            for c in self.categories
        ]
        return {"version": self.version, "tree": tree}


def _finalize(version: int, categories: list[dict], groups: list[dict]) -> Taxonomy:
    """Assemble a :class:`Taxonomy` snapshot + its derived lookup maps."""
    ext_to_group: dict[str, str] = {}
    for g in groups:
        for e in g["extensions"]:
            ext_to_group[e] = g["key"]
    group_to_category = {g["key"]: g["category"] for g in groups}
    category_extractors = {c["key"]: c["extractor"] for c in categories}
    return Taxonomy(
        version=version,
        categories=tuple(categories),
        groups=tuple(groups),
        ext_to_group=ext_to_group,
        group_to_category=group_to_category,
        category_extractors=category_extractors,
    )


def _seed_snapshot() -> Taxonomy:
    """Build the FALLBACK snapshot from the pure seed (:mod:`filearr.file_groups`).
    Used when the DB has no taxonomy rows yet (pre-migration boot / create_all test
    DB) so ``detect`` never hard-fails. Version is the ``_SEED_VERSION`` sentinel."""
    payload = fg.taxonomy_seed_payload()
    ext_by_group: dict[str, list[str]] = {}
    for row in payload["extensions"]:
        ext_by_group.setdefault(row["group"], []).append(row["ext"])
    groups = [
        {**g, "is_builtin": True, "extensions": sorted(ext_by_group.get(g["key"], []))}
        for g in payload["groups"]
    ]
    categories = [{**c, "is_builtin": True} for c in payload["categories"]]
    return _finalize(_SEED_VERSION, categories, groups)


# --------------------------------------------------------------------------- #
# Process-global version-keyed cache                                            #
# --------------------------------------------------------------------------- #
_cache: Taxonomy | None = None
_lock = asyncio.Lock()


def invalidate() -> None:
    """Drop the cached snapshot so the next :func:`load` re-reads the DB. Called by
    the CRUD API after every edit (belt-and-braces alongside the version bump)."""
    global _cache
    _cache = None


async def _current_version(session: AsyncSession) -> int | None:
    """The DB taxonomy version, or ``None`` when the taxonomy is unseeded/unreachable
    (→ caller uses the seed fallback). Best-effort: never raises."""
    try:
        state = (
            await session.execute(select(TaxonomyState.version).limit(1))
        ).scalar_one_or_none()
        return state
    except Exception:
        logger.warning("taxonomy: version probe failed; using seed fallback", exc_info=True)
        return None


async def _load_from_db(session: AsyncSession, version: int) -> Taxonomy:
    """Build a snapshot for DB ``version``. Falls back to the seed on any read error
    or an empty category set (never hard-fails a scan)."""
    try:
        cat_rows = (
            await session.execute(
                select(FileCategoryModel).order_by(
                    FileCategoryModel.sort_order, FileCategoryModel.key
                )
            )
        ).scalars().all()
        if not cat_rows:
            return _seed_snapshot()
        grp_rows = (
            await session.execute(
                select(FileGroupModel).order_by(
                    FileGroupModel.sort_order, FileGroupModel.key
                )
            )
        ).scalars().all()
        ext_rows = (
            await session.execute(
                select(FileGroupExtension.ext, FileGroupExtension.group_id)
            )
        ).all()
    except Exception:
        logger.warning("taxonomy: DB load failed; using seed fallback", exc_info=True)
        return _seed_snapshot()

    cat_key_by_id = {c.id: c.key for c in cat_rows}
    exts_by_group_id: dict[object, list[str]] = {}
    for ext, gid in ext_rows:
        exts_by_group_id.setdefault(gid, []).append(ext)

    categories = [
        {
            "key": c.key,
            "label": c.label,
            "description": c.description or "",
            "extractor": c.extractor,
            "sort_order": c.sort_order,
            "is_builtin": bool(c.is_builtin),
        }
        for c in cat_rows
    ]
    groups = [
        {
            "key": g.key,
            "label": g.label,
            "description": g.description or "",
            "category": cat_key_by_id.get(g.category_id, CATEGORY_OTHER),
            "sort_order": g.sort_order,
            "is_builtin": bool(g.is_builtin),
            "extensions": sorted(exts_by_group_id.get(g.id, [])),
        }
        for g in grp_rows
    ]
    return _finalize(version, categories, groups)


async def load(session: AsyncSession) -> Taxonomy:
    """Return the current taxonomy snapshot, reloading iff the DB version changed.

    Load ONCE per scan/batch and reuse the returned snapshot's pure ``detect`` for
    every file (no per-file DB I/O)."""
    global _cache
    version = await _current_version(session)
    effective = _SEED_VERSION if version is None else version
    cached = _cache
    if cached is not None and cached.version == effective:
        return cached
    async with _lock:
        if _cache is not None and _cache.version == effective:
            return _cache
        snap = (
            _seed_snapshot() if version is None else await _load_from_db(session, version)
        )
        _cache = snap
        return snap


async def detect(session: AsyncSession, path: str) -> tuple[str, str]:
    """Classify ``path`` into ``(category_key, group_key)`` via the current snapshot.
    Convenience wrapper — hot loops should ``load()`` once and call ``.detect``."""
    tax = await load(session)
    return tax.detect(path)


async def category_extractor(session: AsyncSession, category_key: str) -> str | None:
    """The extraction pipeline for ``category_key`` (W8-B routes on this), or
    ``None``."""
    tax = await load(session)
    return tax.extractor_for(category_key)


async def tree(session: AsyncSession) -> dict:
    """The full taxonomy tree for the API (see :meth:`Taxonomy.as_tree`)."""
    tax = await load(session)
    return tax.as_tree()


async def current_version(session: AsyncSession) -> int:
    """The current taxonomy version — the DB ``taxonomy_state.version``, or the seed
    sentinel ``0`` when the taxonomy is unseeded/unreachable. A LIGHT probe (reads
    only the counter, never the tree), so the agent-policy endpoint can fold it into
    the policy ETag cheaply on every poll (W8-E)."""
    version = await _current_version(session)
    return _SEED_VERSION if version is None else version


async def agent_payload(session: AsyncSession) -> dict:
    """The compact agent taxonomy payload for the current snapshot (W8-E). See
    :meth:`Taxonomy.agent_payload`."""
    tax = await load(session)
    return tax.agent_payload()


async def bump_version(session: AsyncSession) -> int:
    """Increment ``taxonomy_state.version`` (cache-invalidation token) and invalidate
    the in-process cache. The caller commits; the bump rides that transaction so the
    version advances atomically with the edit. Returns the new version."""
    from sqlalchemy import update

    await session.execute(
        update(TaxonomyState)
        .where(TaxonomyState.id == 1)
        .values(version=TaxonomyState.version + 1)
    )
    invalidate()
    new_version = (
        await session.execute(select(TaxonomyState.version).where(TaxonomyState.id == 1))
    ).scalar_one_or_none()
    return int(new_version) if new_version is not None else _SEED_VERSION
