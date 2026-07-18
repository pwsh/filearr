"""On-demand content digests (Phase 3, roadmap §5 — P3-T1 hash search).

**Inert scaffolding.** Only tests import this module; nothing in the runtime
wires it in yet. It ships one implemented, unit-tested pure helper
(``compute_digests``) plus the ``HASH_ATTRIBUTES`` constant that P3-T1 will feed
into ``search.py``'s typo-tolerance ``disable_on_attributes`` list.

Why a *separate*, on-demand digest path (not the existing scan-time hashing):
the scan/extract pipeline computes xxh3 ``quick_hash``/``content_hash`` under
T7's network-cost policy (quick-only over SMB/NFS by default). Cryptographic
MD5/SHA-256 are a different, heavier ask — the P0 design (brief §7) is a lazy
``GET /items/{id}/hash?algo=sha256`` endpoint that streams the file once on
request and caches the hex in ``metadata_`` (extracted fact, invariant 2), never
computed eagerly on every scanned file. This module is that streaming helper,
kept pure/synchronous so the API/worker layer decides threading + caching.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

# 1 MiB streaming window: never materialise a whole (multi-GB) media file in RAM.
DEFAULT_CHUNK_SIZE = 1024 * 1024

# Meili attributes that MUST have typo tolerance disabled (P3-T1). A hex digest,
# a bare extension, an epoch mtime, and a UUID FK are all fields where fuzzy /
# typo matching is actively harmful: two hashes differing by one hex digit are
# unrelated files, not near-neighbours. Reconciled with the phase-9 Meili-adoption
# brief, whose ``disable_on_attributes`` recommendation is
# ``["year", "size", "extension", "mtime", "sidecar_of"]`` — ``year``/``size`` are
# numeric and disabled there; this constant carries the hash + string-shaped
# members P3-T1 adds when ``quick_hash``/``content_hash`` become filterable.
HASH_ATTRIBUTES: tuple[str, ...] = (
    "quick_hash",
    "content_hash",
    "extension",
    "mtime",
    "sidecar_of",
)


def compute_digests(
    path: str,
    algorithms: Iterable[str] = ("md5", "sha256"),
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, str]:
    """Stream ``path`` once and return ``{algorithm: hex_digest}``.

    Streaming, bounded-memory: the file is read in ``chunk_size`` windows and fed
    to every requested hasher in a single pass, so an N-GB file costs one read of
    N bytes and O(chunk_size) resident memory — never the whole file in RAM.

    ``algorithms`` are ``hashlib`` names (``md5``, ``sha1``, ``sha256``, ...); an
    unknown name raises ``ValueError`` (from ``hashlib.new``) before any read. An
    empty ``algorithms`` iterable returns ``{}`` without opening the file.

    Pure w.r.t. process state (no caching, no ORM, no network) — the caller owns
    where the result is persisted (P3-T1: ``metadata_`` cache).
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    names: Sequence[str] = list(algorithms)
    if not names:
        return {}
    hashers = {name: hashlib.new(name) for name in names}
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            for h in hashers.values():
                h.update(chunk)
    return {name: h.hexdigest() for name, h in hashers.items()}
