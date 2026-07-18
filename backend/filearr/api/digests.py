"""On-demand cryptographic digest endpoint (P3-T1, roadmap §5 hash search).

A LAZY companion to scan-time hashing. The scan/extract pipeline computes the
cheap xxh3 ``quick_hash``/``content_hash`` under T7's network-cost policy; this
endpoint computes the heavier cryptographic MD5/SHA-256 only when explicitly
asked, streams the file exactly ONCE (``hashx.compute_digests`` in a threadpool),
and caches the hex under ``metadata_.digests`` so a repeat call never re-reads the
file. Caching into ``metadata_`` (the extracted-fact column) is allowed: a digest
is deterministically derived from file content, never a user edit (invariant 2).

Lives in its OWN module (not ``api/items.py``) but mounts under the ``/items``
prefix, so the public route is ``POST /api/v1/items/{id}/digests``.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from filearr.config import get_settings
from filearr.db import get_session
from filearr.hashx import compute_digests
from filearr.models import Item
from filearr.security import require_scope

router = APIRouter()

# Cryptographic digests we allow on demand. Restricting to a fixed allowlist keeps
# an attacker from steering ``hashlib.new`` toward an unexpected/expensive
# algorithm, and gives a clean 422 instead of a 500 from deep in the hasher.
ALLOWED_ALGORITHMS: frozenset[str] = frozenset({"md5", "sha1", "sha256", "sha512"})
DEFAULT_ALGORITHMS: tuple[str, ...] = ("md5", "sha256")

# Where cached hex digests live inside metadata_ (e.g. metadata_["digests"]["sha256"]).
DIGESTS_KEY = "digests"


class _FileTooLarge(Exception):
    """Raised inside the worker thread when the file exceeds the size ceiling."""

    def __init__(self, size: int) -> None:
        self.size = size


def _parse_algorithms(raw: str | None) -> list[str]:
    """Parse the ``algorithms`` query param into a validated, de-duped list.

    Comma-separated, case-insensitive, order-preserving. An empty/absent value
    falls back to the default (md5+sha256). Any name outside the allowlist is a
    422; an all-empty explicit value (e.g. ``","``) is also a 422."""
    if raw is None:
        return list(DEFAULT_ALGORITHMS)
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if name not in ALLOWED_ALGORITHMS:
            raise HTTPException(
                422,
                f"Unsupported digest algorithm {name!r}; "
                f"allowed: {', '.join(sorted(ALLOWED_ALGORITHMS))}",
            )
        if name not in names:
            names.append(name)
    if not names:
        raise HTTPException(422, "No digest algorithm requested")
    return names


def _digest_file_sync(path: str, algorithms: list[str], max_bytes: int) -> dict[str, str]:
    """Stat (size-gate) then stream-hash. Runs in a worker thread.

    The size ceiling is checked from a single ``os.stat`` BEFORE any bytes are
    read, so an oversized file is rejected without a multi-GB read. ``os.stat``
    raises ``FileNotFoundError`` for a missing file, surfaced as a 409 upstream."""
    size = os.stat(path).st_size
    if size > max_bytes:
        raise _FileTooLarge(size)
    return compute_digests(path, algorithms=algorithms)


@router.post(
    "/{item_id}/digests",
    dependencies=[Depends(require_scope("write"))],
)
async def compute_item_digests(
    item_id: uuid.UUID,
    algorithms: str | None = Query(
        default=None,
        description="comma-separated digest algorithms (md5,sha1,sha256,sha512); "
        "defaults to md5,sha256. Cached per-algorithm in metadata_ after the first "
        "computation, so repeat calls do not re-read the file.",
    ),
    session: AsyncSession = Depends(get_session),
) -> dict:
    algos = _parse_algorithms(algorithms)

    item = (
        await session.execute(select(Item).where(Item.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Item not found")

    cached: dict[str, str] = dict((item.metadata_ or {}).get(DIGESTS_KEY, {}))
    result: dict[str, str] = {a: cached[a] for a in algos if a in cached}
    missing = [a for a in algos if a not in cached]

    computed: list[str] = []
    if missing:
        s = get_settings()
        try:
            # Absolute path comes from the item row only (never user input);
            # read-only. Bounded by a size ceiling and a wall-clock timeout so a
            # huge/slow SMB read can neither OOM nor hang the request forever.
            fresh = await asyncio.wait_for(
                run_in_threadpool(
                    _digest_file_sync, item.path, missing, s.digest_max_bytes
                ),
                timeout=s.digest_timeout_s,
            )
        except _FileTooLarge as exc:
            raise HTTPException(
                413,
                f"File is {exc.size} bytes, above the {s.digest_max_bytes}-byte "
                "on-demand digest ceiling (FILEARR_DIGEST_MAX_BYTES)",
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(409, f"Source file not found on disk: {item.path}") from exc
        except TimeoutError as exc:
            raise HTTPException(
                504,
                f"Digest computation exceeded {s.digest_timeout_s}s "
                "(FILEARR_DIGEST_TIMEOUT_S)",
            ) from exc

        result.update(fresh)
        computed = list(fresh)
        # Persist the new digests into metadata_.digests. Reassign (not in-place
        # mutate) so SQLAlchemy flushes the JSONB change; merge to preserve any
        # previously-cached algorithms.
        new_meta = dict(item.metadata_ or {})
        new_meta[DIGESTS_KEY] = {**cached, **fresh}
        item.metadata_ = new_meta
        await session.commit()

    return {"id": str(item.id), "digests": result, "computed": computed}
