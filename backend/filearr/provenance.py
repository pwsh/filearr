"""Provenance helpers (Phase 4, roadmap §7 — P4-T7).

``policy_version`` derives a short, stable fingerprint of the scan-relevant
configuration of the library that owns an item. It is written to
``Item.policy_version`` at extract time so a reader can tell WHICH config version
last extracted the item, and — per the P4-T7 accept criterion — the value CHANGES
whenever any scan-relevant config value changes and is otherwise stable across
byte-identical rescans.

Composition (the fields that actually influence what/how a file is scanned and
extracted):
  * ``hash_policy`` + ``hash_full_max_bytes`` + the global
    ``scan_hash_full_max_bytes`` fallback + ``root_path`` — these determine the
    resolved T7 hash policy (``root_path`` because ``auto`` resolves network vs
    local from it), i.e. whether an item gets a full ``content_hash``.
  * ``enabled_types`` / ``include_globs`` / ``exclude_globs`` /
    ``enabled_presets`` / ``enabled_extension_groups`` — the inclusion controls
    that decide whether the file is scanned at all and as which media type.

The fingerprint is a JSON-canonical, order-insensitive digest (lists sorted) so
reordering an array in the config is not a spurious change. Prefixed with the
scheme version (``cfg2:``) so the composition scheme itself is versioned:
extending the input set — or changing the *hashing implementation* — later bumps
the prefix, making old vs new fingerprints unambiguously distinguishable.

QH-T4 scheme bump: ``cfg1`` → ``cfg2`` for the QH-T1..T3 hashing fix (quick_hash
64-128 KiB partial-read repair, small-file unconditional content_hash, and
content_hash's xxh3-64 → xxh3-128 widening). ``policy_version`` fingerprints
*config*, not hashing *behavior*, so without this bump the fix would ship leaving
every already-stored item's fingerprint unchanged and no way to tell "hashed
under the fixed algorithm" from "hashed under the buggy one". Bumping the scheme
(and folding :data:`HASH_IMPL_VERSION` into the payload) makes that a queryable
predicate — ``policy_version LIKE 'cfg2:%'`` — with no data migration.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from filearr.config import Settings
    from filearr.models import Library

# Fingerprint scheme version — bump when the composed field set below OR the
# hashing implementation changes so a policy_version computed under an old scheme
# never collides with a new one. cfg1 -> cfg2: QH-T1..T3 hashing fix.
_SCHEME = "cfg2"

# Hashing-implementation version, folded into the fingerprint payload (§9.1) so
# ANY future hashing-behavior change also bumps every item's policy_version even
# when the library config is byte-identical. 1 = pre-QH-T1 (buggy 64-128 KiB
# quick_hash; xxh3-64 content_hash). 2 = QH-T1..T3 (whole-file small hashing,
# xxh3-128 content_hash).
HASH_IMPL_VERSION = 2


def _canonical(library: Library, global_hash_full_max_bytes: int) -> dict:
    """The order-insensitive dict the fingerprint hashes. Lists are sorted so a
    pure reordering of an array config value is not treated as a change.

    ``hash_impl_version`` folds the hashing-implementation marker into the payload
    so a hashing-algorithm change bumps the fingerprint even with unchanged config
    (§9.1) — belt-and-suspenders with the ``_SCHEME`` prefix bump."""
    return {
        "hash_impl_version": HASH_IMPL_VERSION,
        "hash_policy": library.hash_policy,
        "hash_full_max_bytes": library.hash_full_max_bytes,
        "global_hash_full_max_bytes": global_hash_full_max_bytes,
        "root_path": library.root_path,
        "enabled_types": sorted(library.enabled_types or []),
        "include_globs": sorted(library.include_globs or []),
        "exclude_globs": sorted(library.exclude_globs or []),
        "enabled_presets": sorted(library.enabled_presets or []),
        "enabled_extension_groups": sorted(library.enabled_extension_groups or []),
    }


def policy_version(library: Library, settings: Settings) -> str:
    """Return the ``cfg2:<16-hex>`` policy fingerprint for ``library``.

    Stable for identical config, changes on any scan-relevant config change. Pure
    (no IO); the 16-hex truncation of a sha256 is ample for a change-detection
    fingerprint (this is not a security digest)."""
    payload = json.dumps(
        _canonical(library, settings.scan_hash_full_max_bytes),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{_SCHEME}:{digest}"
