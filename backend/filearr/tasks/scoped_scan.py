"""Scoped (hot-folder) scanning scaffolding (Phase 2, roadmap §4 / brief §3.6).

Inert scaffolding for P2-T6. The only piece implemented and unit-tested now is
:func:`resolve_scan_path` — the pure longest-``rel_path``-prefix-wins resolver
that the periodic tick and the watch supervisor will use to decide which
per-path override (if any) governs a given path. The scoped-walk entry point is
a stub (P2-T6) because it must reuse the whole-library ``scan._scan_body`` diff
context (move detection, sidecar association) with a subtree write scope, which
touches the live scan pipeline.

No runtime module imports this file yet — only its tests do.

Ruling R3: a scoped scan gets **read-only** access to the full-library existing
item map (for move-detection + sidecar association) but confines writes/diff to
the subtree. Documented fallback if that proves hairy: skip move-detection in
scoped scans and count relocated files as ``move_ambiguous`` (tombstone+recreate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ScanPathLike(Protocol):
    """Structural type for a ``scan_paths`` row (brief §3.6).

    The real table/ORM model is introduced by P2-T6's migration. Any object
    exposing ``rel_path`` (path relative to the library root; ``""`` = the
    library root itself) satisfies the resolver, so it works against both the
    lightweight :class:`ScanPathRule` below and a future SQLAlchemy row.
    """

    rel_path: str


@dataclass(frozen=True)
class ScanPathRule:
    """Lightweight stand-in for a ``scan_paths`` row (pre-migration).

    ``scan_cron``/``watch_mode`` are ``None`` when the row inherits the library
    default (NULL-inherits, matching T7's ``hash_full_max_bytes`` convention).
    """

    rel_path: str
    scan_cron: str | None = None
    watch_mode: bool | None = None
    enabled: bool = True


def _norm(rel: str) -> str:
    """Normalise a rel_path for prefix comparison: strip surrounding slashes."""
    return rel.strip("/")


def resolve_scan_path(
    scan_paths: list[ScanPathLike], rel_path: str
) -> ScanPathLike | None:
    """Return the most specific ``scan_paths`` rule covering ``rel_path``.

    Longest-``rel_path``-prefix-wins on **path-segment boundaries** (brief §3.6):
    a rule covers ``rel_path`` iff its ``rel_path`` equals the target, is a
    parent directory of it, or is the empty string (the library-root override,
    which covers everything). Among all covering rules the one with the longest
    ``rel_path`` wins; ties are impossible because ``(library_id, rel_path)`` is
    unique. Returns ``None`` when no rule covers the path (caller falls back to
    the library-level ``scan_cron``/``watch_mode``).

    Pure and total; does not consult ``enabled`` (the caller decides whether a
    disabled rule should still shadow less-specific ones — kept out of the
    resolver so it stays a pure geometry function).
    """
    target = _norm(rel_path)
    best: ScanPathLike | None = None
    best_len = -1
    for rule in scan_paths:
        base = _norm(rule.rel_path)
        covers = base == "" or target == base or target.startswith(base + "/")
        if not covers:
            continue
        if len(base) > best_len:
            best, best_len = rule, len(base)
    return best


# --- Stub: scoped walk entry point (NOT implemented) -----------------------

async def scan_subtree(library_id: str, rel_path: str) -> dict:
    """P2-T6: run a scan confined to ``rel_path`` within a library.

    Walks only ``os.path.join(root_path, rel_path)`` but diffs against the
    library's full existing-item map (read-only) so move detection and sidecar
    association keep whole-library context, while writes/tombstones stay within
    the subtree (R3). Emits the same ``ScanRun.stats`` shape as a full scan, with
    an added ``scope`` key. Delegates to :func:`scan.scan_library` (the one scan
    pipeline) with the subtree scope rather than duplicating the walk/diff logic.

    Imported lazily to avoid a circular import (``scan`` imports ``worker`` which
    imports the task modules).
    """
    from filearr.tasks.scan import scan_library

    return await scan_library(library_id, rel_path=rel_path)
