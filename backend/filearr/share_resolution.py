"""P10-T11/T12 — the single item→network-share resolver (precedence, R1).

One function, :func:`resolve_item_share`, answers "what network location opens
this item, and where did that answer come from?" with a FROZEN precedence:

    agent share_hint  >  agent_share_maps mapping  >  library.share_prefix  >  none

- **agent_hint** (P10-T11) — a fresh, present agent-reported ``share_hint`` on the
  item wins: the agent that actually hosts the file is the most authoritative
  source of how to reach it over its own network. Absence is the normal case (R1).
- **mapping** (P10-T12) — an admin-defined ``agent_share_maps`` rule for the
  hosting agent (longest-``local_prefix``-wins, agent-scoped outranking global),
  the deterministic fallback when the agent self-reports nothing. Only consulted
  for an agent-hosted item (``source_agent_id`` set).
- **library** — the existing OPS-T7 / library ``share_prefix`` path
  (:func:`filearr.share_map.item_share_url`): a manual library prefix or the
  deploy mount map. Covers a centrally-scanned item (the common case) and an
  agent library that an operator gave an explicit ``share_prefix``.
- **none** — no location applies; the caller renders no network-open link (never a
  fabricated location — accept criterion).

Pure and total: no I/O, no DB. The API layer loads the applicable
``agent_share_maps`` rows and passes them as :class:`filearr.transfers.ShareMapping`
values. Returns ``(share_url, source)`` where ``source`` is one of
``"agent_hint"`` / ``"mapping"`` / ``"library"`` / ``None``.

**Search-projection decision (frozen this round):** share_url is resolved at
DISPLAY time only — it is NOT added to the Meilisearch document. A hint/mapping can
change (a new admin rule, a fresh agent report) without any item mutation, so
baking it into the disposable index would make it stale and force a reindex on
every share-map edit; the item-detail payload computes it live instead. See
``search.build_doc`` (unchanged) and ``api.items.get_item``.
"""

from __future__ import annotations

from typing import Literal

from filearr import share_map
from filearr.transfers import ShareMapping, resolve_share_url

ShareSource = Literal["agent_hint", "mapping", "library"]


def _agent_local_path(root_path: str, rel_path: str) -> str:
    """Reconstruct the item's absolute path AS THE AGENT'S OS SEES IT: the agent
    library root (``library.root_path`` == the agent-side absolute root, stored
    verbatim by ``agentsync._provision_agent_library``) joined with the item's
    ``rel_path``. Separator-normalisation is left to the resolver
    (:func:`resolve_share_url` → ``_norm_local``), so a Windows ``C:\\media`` root
    and a posix rel_path compose correctly."""
    base = root_path.replace("\\", "/").rstrip("/")
    rel = rel_path.replace("\\", "/").strip("/")
    return f"{base}/{rel}" if rel else base


def resolve_item_share(
    *,
    share_hint: dict | None,
    source_agent_id: str | None,
    agent_mappings: list[ShareMapping],
    library_share_prefix: str | None,
    library_root_path: str,
    item_path: str,
    rel_path: str,
) -> tuple[str | None, ShareSource | None]:
    """Resolve one item to ``(share_url, source)`` per the frozen precedence.

    ``share_hint`` is the item's stored agent hint (``items.share_hint`` JSONB, or
    None). ``source_agent_id`` is the hosting agent (None for a centrally-scanned
    item — the mapping tier is then skipped). ``agent_mappings`` are the
    ``agent_share_maps`` rows applicable to that agent (agent-scoped + global);
    for a central item pass ``[]``. ``library_share_prefix`` / ``library_root_path``
    / ``item_path`` / ``rel_path`` feed the library-tier
    :func:`filearr.share_map.item_share_url` exactly as it is called today."""
    # 1. Agent hint — fresh, present, most authoritative (R1).
    if share_hint:
        url = share_hint.get("share_url")
        if isinstance(url, str) and url:
            return url, "agent_hint"

    # 2. Central agent_share_maps mapping — deterministic fallback for an
    #    agent-hosted item, keyed on the agent-local absolute path.
    if source_agent_id is not None and agent_mappings:
        url = resolve_share_url(
            agent_mappings,
            source_agent_id,
            _agent_local_path(library_root_path, rel_path),
        )
        if url:
            return url, "mapping"

    # 3. Library share_prefix / deploy mount map (existing OPS-T7 behaviour).
    url = share_map.item_share_url(library_share_prefix, item_path, rel_path)
    if url:
        return url, "library"

    return None, None
