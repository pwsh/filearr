"""Sidecar association pass (T3).

Runs after the scan walk has committed all item rows for a library. Recomputes
``items.sidecar_of`` for every active item from scratch (idempotent) and, for
Kodi ``.nfo`` sidecars, folds parsed metadata into the *parent's* extracted
``metadata`` column. Parent resolution:

  * per-stem sidecar  → the sibling non-sidecar item in the SAME directory whose
    stem matches (``Movie (2020)-thumb.jpg`` → ``Movie (2020).mkv``);
  * ``<stem>.nfo``     → same, matching stem;
  * directory artwork / ``movie.nfo`` / ``tvshow.nfo`` → the directory's *primary*
    media item (largest media file in that directory, deterministic tie-break).

Because it derives purely from the current row set, a rescan, rename, or
sidecar-seen-before-parent ordering all converge to the same result.
"""

from __future__ import annotations

import os

from sqlalchemy import select

from filearr.models import Item, ItemStatus, MediaType
from filearr.nfo import parse_nfo_bytes
from filearr.sidecar import classify

# Media types considered "primary" candidates for directory-level artwork.
_PRIMARY_TYPES = frozenset(
    {
        MediaType.video, MediaType.audio, MediaType.audiobook,
        MediaType.sample, MediaType.model3d, MediaType.document,
        MediaType.spreadsheet, MediaType.image,
    }
)


def _dir_of(rel_path: str) -> str:
    return os.path.dirname(rel_path)


def _stem_of(rel_path: str) -> str:
    return os.path.splitext(os.path.basename(rel_path))[0].lower()


def resolve_links(items: list[Item]) -> dict[str, str | None]:
    """Pure planner: given the active item rows for a library, return a map of
    {sidecar_item_id: parent_item_id_or_None}. Does not touch the DB.

    Only sidecars appear as keys. A sidecar whose parent can't be resolved maps to
    None (kept, but still marked a sidecar via a self-noop? no — see caller: an
    unresolved sidecar stays sidecar_of=None and thus visible; the classify() call
    result is what the caller uses to force-hide). Here we only assign real parents.
    """
    # Index non-sidecar candidates by (dir, stem) and by directory (for primary).
    by_dir_stem: dict[tuple[str, str], Item] = {}
    dir_primaries: dict[str, list[Item]] = {}

    classified: dict[str, object] = {}
    for it in items:
        info = classify(it.rel_path)
        classified[str(it.id)] = info
        if info is None:  # a real media file — eligible parent
            key = (_dir_of(it.rel_path), _stem_of(it.rel_path))
            # Prefer a primary media type on stem collision (e.g. .mkv over .srt).
            existing = by_dir_stem.get(key)
            if existing is None or (
                it.media_type in _PRIMARY_TYPES and existing.media_type not in _PRIMARY_TYPES
            ):
                by_dir_stem[key] = it
            if it.media_type in _PRIMARY_TYPES:
                dir_primaries.setdefault(_dir_of(it.rel_path), []).append(it)

    def primary_for(directory: str) -> Item | None:
        cands = dir_primaries.get(directory)
        if not cands:
            return None
        # Deterministic: largest file, tie-break on rel_path.
        return max(cands, key=lambda i: (i.size or 0, i.rel_path))

    links: dict[str, str | None] = {}
    for it in items:
        info = classified[str(it.id)]
        if info is None:
            continue
        parent: Item | None = None
        if info.parent_stem is not None:  # type: ignore[union-attr]
            key = (info.directory, info.parent_stem.lower())  # type: ignore[union-attr]
            parent = by_dir_stem.get(key)
            if parent is None:
                # Fall back to directory primary if the exact stem sibling is gone.
                parent = primary_for(info.directory)  # type: ignore[union-attr]
        else:
            parent = primary_for(info.directory)  # type: ignore[union-attr]
        # Never let a sidecar point at itself.
        if parent is not None and parent.id != it.id:
            links[str(it.id)] = str(parent.id)
        else:
            links[str(it.id)] = None
    return links


async def associate_sidecars(session, library_id) -> dict[str, int]:
    """Recompute sidecar links for a library and parse NFO metadata into parents.

    Returns stats {'sidecars': n, 'linked': n, 'nfo_parsed': n}.
    """
    items = list(
        (
            await session.execute(
                select(Item).where(
                    Item.library_id == library_id,
                    Item.status == ItemStatus.active,
                )
            )
        ).scalars()
    )
    by_id = {str(i.id): i for i in items}
    links = resolve_links(items)

    sidecars = 0
    linked = 0
    nfo_parsed = 0
    touched_parents: set[str] = set()

    for sid, pid in links.items():
        sidecar = by_id[sid]
        sidecars += 1
        # Update FK only on change (keeps rescans cheap / idempotent).
        current = str(sidecar.sidecar_of) if sidecar.sidecar_of else None
        if current != pid:
            sidecar.sidecar_of = pid
        if pid is not None:
            linked += 1

    # Parse NFO sidecars into their parent's extracted metadata.
    for sid, pid in links.items():
        if pid is None:
            continue
        sidecar = by_id[sid]
        info = classify(sidecar.rel_path)
        if info is None or info.kind != "nfo":
            continue
        parent = by_id.get(pid)
        if parent is None:
            continue
        try:
            # Small NFO read; mirrors the synchronous file IO the extract task
            # already uses. Media mounts are read-only and NFO files are tiny.
            with open(sidecar.path, "rb") as fh:  # noqa: ASYNC230
                data = fh.read()
        except OSError:
            continue
        parsed = parse_nfo_bytes(data)
        if not parsed:
            continue
        nfo_parsed += 1
        ext_ids = parsed.pop("external_ids", None)
        # Extractors write ONLY metadata_ (never user_metadata). Merge, don't clobber
        # sibling extracted keys; NFO is authoritative for the keys it provides.
        parent.metadata_ = {**parent.metadata_, **{f"nfo_{k}": v for k, v in parsed.items()}}
        # Promote a few high-value fields to typed columns when still empty.
        if not parent.title and parsed.get("title"):
            parent.title = str(parsed["title"])
        if not parent.year and parsed.get("year"):
            parent.year = int(parsed["year"])
        if ext_ids:
            parent.external_ids = {**parent.external_ids, **ext_ids}
        touched_parents.add(pid)

    return {
        "sidecars": sidecars,
        "linked": linked,
        "nfo_parsed": nfo_parsed,
        "parents_updated": len(touched_parents),
    }
