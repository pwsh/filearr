"""Per-library hash policy resolution (T7).

A single, pure resolver maps a library's ``hash_policy`` + ``hash_full_max_bytes``
(and, for ``auto``, its filesystem class) onto the two facts every hashing site
needs to know:

  * ``compute_content`` -- whether to compute the expensive full ``content_hash``
    (whole-file xxh3 stream) at all;
  * ``full_max_bytes`` -- the size ceiling above which even a ``full`` policy skips
    the content hash (a 40 GB file is never worth streaming).

quick_hash (bounded ~128 KiB head+tail read) is ALWAYS computed and is not gated
here -- it powers move-detection tier 1 and is cheap even over SMB/NFS.

The policy is resolved ONCE per scan run (the network probe is a mountinfo parse
we do not want to repeat per file) and stashed in ``ScanRun.stats['hash_policy']``
for observability. The per-file extract worker runs in a *separate* process from
the scan, so it re-resolves from the library row (cheap; correctness over a
micro-optimisation) using this same function -- there is one definition of the
policy, not two.
"""

from __future__ import annotations

from dataclasses import dataclass

from filearr.models import HashPolicy
from filearr.schedule import is_network_path


@dataclass(frozen=True)
class ResolvedHashPolicy:
    """The effective, per-file-agnostic hashing decision for one scan run."""

    policy: str          # the resolved *behaviour*: 'full' | 'quick_only'
    declared: str        # the library's declared policy: 'auto' | 'full' | 'quick_only'
    compute_content: bool
    full_max_bytes: int
    network: bool | None  # detected fs class for 'auto'; None when not probed

    def as_stats(self) -> dict:
        """JSON-safe snapshot for ScanRun.stats (observability)."""
        return {
            "declared": self.declared,
            "resolved": self.policy,
            "compute_content": self.compute_content,
            "full_max_bytes": self.full_max_bytes,
            "network": self.network,
        }


def resolve_hash_policy(
    *,
    declared: str,
    root_path: str,
    hash_full_max_bytes: int | None,
    global_max_bytes: int,
    mountinfo: str | None = None,
) -> ResolvedHashPolicy:
    """Resolve a library's declared policy into a concrete hashing decision.

    ``declared`` is the library's ``hash_policy`` text; an unknown value fails SAFE
    to ``auto`` (never crashes a scan over a bad enum). For ``auto`` the filesystem
    is probed via :func:`filearr.schedule.is_network_path` (``mountinfo`` is a test
    injection seam); network -> quick_only behaviour, local -> full behaviour.

    ``full_max_bytes`` is the per-library override when set (and positive),
    otherwise the global ceiling. A non-positive/None override falls back to the
    global value -- validation of the override happens at the API layer, and we
    never let a bad ceiling silently disable all full hashing here.
    """
    try:
        pol = HashPolicy(declared)
    except ValueError:
        pol = HashPolicy.auto

    ceiling = (
        hash_full_max_bytes
        if hash_full_max_bytes is not None and hash_full_max_bytes > 0
        else global_max_bytes
    )

    network: bool | None = None
    if pol is HashPolicy.auto:
        network = is_network_path(root_path, mountinfo)
        effective = HashPolicy.quick_only if network else HashPolicy.full
    else:
        effective = pol

    return ResolvedHashPolicy(
        policy=effective.value,
        declared=pol.value,
        compute_content=effective is HashPolicy.full,
        full_max_bytes=ceiling,
        network=network,
    )
