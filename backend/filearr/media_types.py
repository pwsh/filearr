"""Legacy extension→bucket helpers, reduced to a thin shim over the taxonomy.

W8-B removed the ``MediaType`` enum and routed classification through the
DB-backed File Extension Similarity Taxonomy (:mod:`filearr.taxonomy`) with its
pure seed fallback in :mod:`filearr.file_groups`. This module now only re-exports
the seed classifiers so any lingering ``media_types.detect`` import keeps working
by delegating to the taxonomy seed (``file_category`` / ``file_group``), rather
than the deleted enum.

AGENT PARITY NOTE: the Go agent keeps its OWN copy of the extension→type map
(``agent/internal/...``); it is untouched by this wave and does not import this
module. When the agent adopts the taxonomy, repoint it at the seed payload
(``file_groups.taxonomy_seed_payload``) rather than resurrecting a Python enum.
"""

from filearr.file_groups import detect_category, detect_group

# Back-compat aliases: callers that historically wanted "the bucket for a path"
# now get the taxonomy ``file_category`` (the successor to the old MediaType).
detect = detect_category

__all__ = ["detect", "detect_category", "detect_group"]
