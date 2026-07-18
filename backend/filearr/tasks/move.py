"""Move/rename detection (T2).

Runs at scan end, *before* tombstoning, and *before* sidecar association. When a
file is renamed or relocated between scans it appears to the diff as one vanished
item (its old ``rel_path`` was not seen) and one brand-new item (the new
``rel_path``). Left alone, the diff would tombstone the old row and create a fresh
one, losing the original identity: id, tags, ``user_metadata``, ``external_ids``,
``first_seen``. This pass reunites the two: it transfers identity onto the
surviving *original* row and drops the freshly-inserted duplicate, so the item id
and every user edit survive the move.

Matching tiers (research-backed, cheap -> strict):
  1. ``(quick_hash, size)`` -- quick_hash is first+last 64 KiB xxh3, so collisions
     are plausible; size narrows but does not eliminate them.
  2. ``content_hash`` confirmation -- when BOTH sides carry a full hash, a mismatch
     *vetoes* the match outright (different bytes, coincidental quick_hash+size).

Integrity first (architect decision, integrity > reliability > speed):
  * A match is transferred ONLY when it is unambiguous. If a ``(quick_hash, size)``
    bucket holds more than one vanished candidate or more than one new item and
    ``content_hash`` cannot single out exactly one pair, NOTHING in that bucket is
    transferred -- the items fall back to the normal tombstone+create path. The
    counts land in ``ScanRun.stats`` (``moved`` / ``move_ambiguous``).
  * Sidecar rows never carry a quick_hash (T3 skips their extraction), so they are
    naturally excluded here; sidecar re-linking after a move is handled by the T3
    association pass, which runs *after* this transfer so it sees surviving ids.

Ordering / the unique index ``(library_id, rel_path)``:
  New-item rows are inserted during the walk with their *new* rel_path, so the
  target rel_path is already occupied by the duplicate we are about to delete.
  Transfer therefore deletes the duplicate first (freeing the rel_path), flushes,
  then rewrites the surviving row's identity columns. Swap cases (A->B while B->A)
  fall out for free: each direction is an independent (quick_hash, size) bucket
  handled in isolation, and because the duplicate is deleted before the survivor
  is repointed, no two live rows ever hold the same rel_path mid-transfer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from filearr import rbac
from filearr.models import Item
from filearr.tasks.extract import full_hash, quick_hash


@dataclass
class MovePlan:
    """A confirmed, unambiguous rename: keep ``survivor`` (the original row, id
    preserved), delete ``duplicate`` (the row inserted this scan), repointing the
    survivor at the duplicate's new location."""

    survivor: Item  # original (candidate tombstone) -- identity kept
    duplicate: Item  # freshly-created row for the new path -- removed


@dataclass
class MovedOut:
    """A confirmed relocation, surfaced to the scan pipeline (P8-T5 alert capture).

    ``detect_moves`` appends one of these per transferred rename when the caller
    passes a ``moved_out`` collector. It carries exactly what the alert layer
    needs: the surviving item's id (identity preserved across the move), its NEW
    ``rel_path``, and the id of the freshly-inserted duplicate row that was
    dropped — so the scan can (a) suppress the spurious ``created`` alert draft
    the duplicate produced during the walk and (b) emit a ``moved`` event for the
    survivor's new location. Purely additive: the ``detect_moves`` return contract
    is unchanged."""

    survivor_id: str
    survivor_rel_path: str
    duplicate_id: str
    library_id: str | None


def _key(item: Item) -> tuple[str, int] | None:
    if item.quick_hash is None or item.size is None:
        return None
    return (item.quick_hash, item.size)


def _content_match(a: Item, b: Item) -> bool | None:
    """Tri-state: True/False when BOTH sides have a content_hash, else None
    (unknown -- cannot confirm nor veto)."""
    if a.content_hash is not None and b.content_hash is not None:
        return a.content_hash == b.content_hash
    return None


def plan_moves(
    candidates: list[Item], new_items: list[Item]
) -> tuple[list[MovePlan], int]:
    """Pure planner. ``candidates`` = vanished original rows that carry a
    quick_hash; ``new_items`` = rows created this scan that carry a quick_hash.

    Returns ``(plans, ambiguous)``. ``plans`` are unambiguous 1:1 renames;
    ``ambiguous`` counts new items that had a plausible (quick_hash, size) match
    but could not be resolved to exactly one candidate (and are thus left to
    tombstone+create).
    """
    cand_buckets: dict[tuple[str, int], list[Item]] = {}
    for c in candidates:
        k = _key(c)
        if k is not None:
            cand_buckets.setdefault(k, []).append(c)

    new_buckets: dict[tuple[str, int], list[Item]] = {}
    for n in new_items:
        k = _key(n)
        if k is not None:
            new_buckets.setdefault(k, []).append(n)

    plans: list[MovePlan] = []
    ambiguous = 0

    for key, news in new_buckets.items():
        cands = cand_buckets.get(key)
        if not cands:
            continue  # genuinely new file -- no vanished twin

        if len(news) == 1 and len(cands) == 1:
            n, c = news[0], cands[0]
            # content_hash may VETO (bytes differ despite quick_hash+size collision)
            if _content_match(c, n) is False:
                ambiguous += 1
                continue
            plans.append(MovePlan(survivor=c, duplicate=n))
            continue

        # Multi-way bucket: only rescue pairs that content_hash pins down to a
        # unique partner on BOTH sides; anything else is ambiguous -> skip.
        remaining_cands = list(cands)
        for n in news:
            confirmed = [c for c in remaining_cands if _content_match(c, n) is True]
            if len(confirmed) == 1:
                c = confirmed[0]
                rival_news = [
                    m for m in news if m is not n and _content_match(c, m) is True
                ]
                if rival_news:
                    ambiguous += 1
                    continue
                plans.append(MovePlan(survivor=c, duplicate=n))
                remaining_cands.remove(c)
            else:
                ambiguous += 1
    return plans, ambiguous


def _ensure_hashes(item: Item, full_max_bytes: int, compute_content: bool) -> None:
    """Compute quick_hash (and, when the T7 policy allows and the file fits the
    size ceiling, content_hash) for a row that has not been through extraction yet.
    New-item rows are hashed on demand here so move detection can run at scan end
    without waiting on the extract queue. IO errors (dead mount, race) leave the
    hashes None -> the item simply won't match and falls through to normal creation.

    When ``compute_content`` is False (quick_only policy, e.g. a network library),
    content_hash is deliberately left None: such a library CANNOT content-confirm a
    move, so a (quick_hash, size) collision that would need content_hash to
    disambiguate is refused by plan_moves and counted as move_ambiguous -- integrity
    is preserved, we simply never falsely transfer identity without proof."""
    if item.quick_hash is not None:
        return
    try:
        item.quick_hash = quick_hash(item.path, item.size)
        if compute_content and item.size is not None and item.size <= full_max_bytes:
            item.content_hash = full_hash(item.path, item.size)
    except OSError:
        pass


async def detect_moves(
    session,
    candidates: list[Item],
    new_items: list[Item],
    full_max_bytes: int,
    compute_content: bool = True,
    moved_out: list[MovedOut] | None = None,
) -> dict[str, int]:
    """Compute hashes for the new rows, match against vanished candidates, and
    transfer identity for unambiguous renames.

    ``candidates`` MUST already carry quick_hash (they are prior-scan rows). Only
    the new rows are hashed here. ``compute_content`` reflects the library's
    resolved T7 hash policy: when False (quick_only), new rows get no content_hash
    and ambiguous (quick_hash, size) buckets that need it stay ambiguous rather
    than transferring on weaker evidence. Returns ``{'moved': n,
    'move_ambiguous': n}``.
    """
    if not candidates or not new_items:
        return {"moved": 0, "move_ambiguous": 0}

    for n in new_items:
        _ensure_hashes(n, full_max_bytes, compute_content)

    plans, ambiguous = plan_moves(candidates, new_items)

    now = datetime.now(UTC)

    # The unique index (library_id, rel_path) makes a naive "repoint survivor onto
    # the duplicate's rel_path" unsafe in *cyclic* renames (A->B while B->C): the
    # target rel_path can still be held by another vanished-but-not-yet-moved row.
    # Three phases inside one transaction avoid every transient collision:
    #   1. delete all duplicate rows, freeing the target rel_paths they occupied;
    #   2. park every survivor at a guaranteed-unique sentinel rel_path (so no two
    #      survivors, and no survivor-vs-lingering-original, ever clash);
    #   3. rewrite survivors to their final rel_paths + identity columns.
    for plan in plans:
        await session.delete(plan.duplicate)
    await session.flush()

    for i, plan in enumerate(plans):
        # Sentinel is unique per survivor (uuid pk) and shaped so it cannot collide
        # with any scanned rel_path: it is absolute-anchored with a private marker
        # segment that os.path.relpath never emits. (Postgres text forbids NUL, so
        # a NUL sentinel is not an option.)
        plan.survivor.rel_path = f"\uffff__filearr_move_pending__/{i}/{plan.survivor.id}"
    await session.flush()

    for plan in plans:
        s, d = plan.survivor, plan.duplicate
        # Identity columns (id, tags, user_metadata, external_ids, first_seen,
        # title/year, metadata_) are deliberately left untouched — only the
        # location + freshly-computed hashes move onto the surviving original.
        s.path = d.path
        s.rel_path = d.rel_path
        # P6-T2: the item moved to a new rel_path -> restamp its ltree scope key.
        s.path_scope = rbac.path_to_ltree(d.rel_path, library_id=s.library_id)
        s.filename = d.filename
        s.extension = d.extension
        s.size = d.size
        s.mtime = d.mtime
        s.status = d.status  # active
        s.last_seen = now
        s.quick_hash = d.quick_hash
        s.content_hash = d.content_hash
        if moved_out is not None:
            # survivor now carries the NEW rel_path; duplicate is about to be gone.
            moved_out.append(
                MovedOut(
                    survivor_id=str(s.id),
                    survivor_rel_path=s.rel_path,
                    duplicate_id=str(d.id),
                    library_id=str(s.library_id) if s.library_id is not None else None,
                )
            )
    await session.flush()

    return {"moved": len(plans), "move_ambiguous": ambiguous}
