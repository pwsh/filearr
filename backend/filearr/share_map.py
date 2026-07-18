"""OPS-T7 — deploy-time network-share mount map → auto ``share_prefix``.

The Proxmox deploy wizard (:file:`proxmox/deploy-proxmox.sh`, ``setup_storages``)
rclone/NFS-mounts each configured network storage INSIDE the LXC at a
container-visible path (e.g. ``/data/media/media``) and — because it alone knows
the true share URL behind every mount — writes a small, **credential-free** map
to :data:`Settings.share_map_path` (``/config/share-map.json``, bind-mounted into
the app). This module loads that map and resolves a container path back to the
user-facing network location, so a library's ``share_prefix`` auto-populates from
the deploy and *stays* correct across remounts/redeploys — no hand-maintenance.

Design (mirrors the P10-T12 central-mapping shape in :mod:`filearr.transfers`,
reusing its pure longest-prefix helpers):

- **Disposable + best-effort.** A missing / unreadable / malformed file is not an
  error: it yields an empty map (logged once) and every ``resolve`` returns
  ``None`` — libraries simply fall back to their manual ``share_prefix`` (or no
  open-location affordance). The app NEVER crashes on a bad map.
- **Manual override wins.** This module only supplies a *fallback*: a library's
  explicit ``library.share_prefix`` always takes precedence (see
  :func:`effective_library_share`). Callers use the map only when no manual prefix
  is set, so a remap stays live without any DB migration.
- **Credential-free.** ``share_url`` is a user-facing UNC-ish reference only; the
  deploy script deliberately omits every username/password.
- **mtime cache.** The parsed map is cached and re-read only when the file's
  mtime changes, so a redeploy that rewrites the file is picked up live (the
  deploy also recreates the container, but this keeps a hot reconfigure correct).
"""

from __future__ import annotations

import json
import logging
import os

from pydantic import BaseModel, ConfigDict, ValidationError

from filearr.config import get_settings

# Reuse the vetted, pure longest-prefix + separator-safe join discipline from the
# P10 share-location core (same geometry as ``resolve_scan_path`` / pathlinks).
from filearr.transfers import ShareScheme, _join_share, _norm_local, classify_prefix

log = logging.getLogger("filearr.share_map")


class ShareMapEntry(BaseModel):
    """One deploy-written mount→share mapping (``/config/share-map.json``).

    ``container_prefix`` is the path the app sees (an rclone/NFS mountpoint like
    ``/data/media/media``); ``share_url`` is the user-facing network location
    behind it (``smb://host/share/sub``, ``sftp://host/path``, ``host:/export``…).
    ``unc`` is the Windows-friendly ``\\\\host\\share`` variant for SMB (absent for
    other types). ``storage_type`` / ``host`` are informational. Unknown keys are
    ignored (forward-compatible)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    container_prefix: str
    share_url: str
    storage_type: str | None = None
    host: str | None = None
    unc: str | None = None


class ShareHint(BaseModel):
    """Resolution result for a container path: the network location that opens it.

    ``share_url`` is the ``share_url`` prefix joined with the leftover path
    segments (native separators via :func:`filearr.transfers._join_share`); ``unc``
    is the same for the Windows variant when the source mapping carried one.
    ``scheme`` classifies ``share_url`` the same way the frontend ``pathlinks``
    does, so a server-computed value and the browser agree."""

    model_config = ConfigDict(frozen=True)

    share_url: str
    scheme: ShareScheme = "unknown"
    unc: str | None = None
    storage_type: str | None = None
    host: str | None = None


class ShareLocation(BaseModel):
    """UI-T15 — both openable renderings of one network location.

    ``url`` is the URL-scheme form the way a Linux/mac client opens it
    (``smb://host/share/sub``, ``sftp://host/path``, a POSIX mount point, …);
    ``unc`` is the Windows form (``\\\\host\\share\\sub``). A location that has
    no UNC representation (any non-SMB scheme, a POSIX mount, an unclassifiable
    prefix) carries ``unc=None`` — the caller then falls back to ``url``. Either
    field may be ``None`` when no location applies at all."""

    model_config = ConfigDict(frozen=True)

    url: str | None = None
    unc: str | None = None


# --- load + validate (never raises) ----------------------------------------

# mtime-keyed cache: (mtime_or_sentinel, entries). ``-1.0`` marks "file absent"
# so we do not re-stat-miss-log every call. ``None`` = never loaded.
_cache: tuple[float, list[ShareMapEntry]] | None = None
_missing_logged = False


def _parse(raw: str, path: str) -> list[ShareMapEntry]:
    """Parse + validate the JSON text into entries. Malformed top-level JSON or a
    non-list payload → empty (warned once). Individual bad rows are skipped with a
    warning; valid rows are kept (partial map is better than none)."""
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("share map %s is not valid JSON (%s) — ignoring", path, exc)
        return []
    if not isinstance(data, list):
        log.warning("share map %s must be a JSON array — ignoring", path)
        return []
    entries: list[ShareMapEntry] = []
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            log.warning("share map %s[%d] is not an object — skipped", path, i)
            continue
        try:
            entry = ShareMapEntry.model_validate(row)
        except ValidationError as exc:
            log.warning("share map %s[%d] invalid (%s) — skipped", path, i, exc)
            continue
        # A blank prefix or url is meaningless (would match everything / open
        # nothing) — drop it defensively.
        if not entry.container_prefix.strip() or not entry.share_url.strip():
            log.warning("share map %s[%d] has empty container_prefix/share_url — skipped", path, i)
            continue
        entries.append(entry)
    return entries


def get_entries() -> list[ShareMapEntry]:
    """Return the current mount map, (re)loading only when the file changes.

    Missing / unreadable file → empty list (logged once). Never raises."""
    global _cache, _missing_logged
    path = get_settings().share_map_path
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if _cache is not None and _cache[0] == -1.0:
            return _cache[1]
        if not _missing_logged:
            log.info("share map %s not present — auto share_prefix disabled", path)
            _missing_logged = True
        _cache = (-1.0, [])
        return []
    if _cache is not None and _cache[0] == mtime:
        return _cache[1]
    try:
        raw = open(path, encoding="utf-8").read()  # noqa: SIM115
    except OSError as exc:
        log.warning("share map %s unreadable (%s) — ignoring", path, exc)
        _cache = (mtime, [])
        return []
    entries = _parse(raw, path)
    _missing_logged = False
    log.info("share map %s loaded: %d mapping(s)", path, len(entries))
    _cache = (mtime, entries)
    return entries


def reset_cache() -> None:
    """Drop the cache (tests / explicit reload)."""
    global _cache, _missing_logged
    _cache = None
    _missing_logged = False


# --- resolution (pure; longest-container_prefix-wins) ----------------------


def resolve(path: str, entries: list[ShareMapEntry] | None = None) -> ShareHint | None:
    """Resolve a container ``path`` to a :class:`ShareHint`, or ``None`` when no
    mapping covers it.

    Longest-``container_prefix``-wins on path-segment boundaries (same discipline
    as :func:`filearr.transfers.resolve_share_url`): a mapping covers ``path`` iff
    its ``container_prefix`` equals the path or is a parent directory of it. The
    leftover segments are appended to both ``share_url`` and ``unc`` using each
    prefix's native separator. Case-preserving + separator-safe. Pure (no I/O when
    ``entries`` is supplied)."""
    if entries is None:
        entries = get_entries()
    target = _norm_local(path)
    best: ShareMapEntry | None = None
    best_len = -1
    for e in entries:
        base = _norm_local(e.container_prefix)
        covers = base == "" or target == base or target.startswith(base + "/")
        if covers and len(base) > best_len:
            best, best_len = e, len(base)
    if best is None:
        return None
    base_segments = [s for s in _norm_local(best.container_prefix).split("/") if s]
    target_segments = [s for s in target.split("/") if s]
    remainder = target_segments[len(base_segments) :]
    share_url = _join_share(best.share_url, remainder)
    unc = _join_share(best.unc, remainder) if best.unc else None
    return ShareHint(
        share_url=share_url,
        scheme=classify_prefix(best.share_url),
        unc=unc,
        storage_type=best.storage_type,
        host=best.host,
    )


def effective_library_share(
    share_prefix: str | None, root_path: str
) -> tuple[str | None, str]:
    """Effective library-root share prefix + its source.

    Returns ``(value, source)`` where ``source`` is:

    * ``"manual"`` — the library's explicit ``share_prefix`` (wins whenever set);
    * ``"mount-map"`` — auto-derived from the deploy mount map covering the
      library ``root_path`` (the resolved network location of the root);
    * ``"none"`` — neither (no open-location affordance).

    The returned value is a library-ROOT prefix (item ``rel_path`` is appended by
    the UI / reports), so a manual and an auto value are interchangeable at every
    consumer. Computed at read time, so a remap goes live with no migration."""
    if share_prefix:
        return share_prefix, "manual"
    hint = resolve(root_path)
    if hint is not None:
        return hint.share_url, "mount-map"
    return None, "none"


def item_share_url(share_prefix: str | None, item_path: str, rel_path: str) -> str | None:
    """Effective network-open URL for a SINGLE item (reports / row context).

    Manual ``share_prefix`` wins — joined with ``rel_path`` via the shared
    separator-safe joiner. Otherwise the deploy mount map resolves the item's
    absolute container ``item_path`` directly. ``None`` when neither applies (no
    fabricated location)."""
    if share_prefix:
        segs = [s for s in rel_path.replace("\\", "/").split("/") if s]
        return _join_share(share_prefix, segs)
    hint = resolve(item_path)
    return hint.share_url if hint is not None else None


# --- UI-T15: SMB URL <-> UNC derivation + both-format resolution -----------
#
# A single network location can be typed/served in two OS-native ways: a URL
# scheme (``smb://host/share/sub`` — Linux/macOS) or a Windows UNC
# (``\\host\share\sub``). The two are inter-derivable **only for SMB**; every
# other URL scheme (sftp/ftp/nfs/webdav/file) has no UNC form. Derivation is
# separator-only (segments are preserved verbatim, spaces included — same
# discipline as :func:`filearr.transfers._join_share`) and NEVER emits
# credentials or a port (UNC cannot carry either).


def _derive_unc_from_url(url: str) -> str | None:
    """``smb://host/share/sub`` → ``\\host\\share\\sub`` (Windows UNC).

    Returns ``None`` when no faithful UNC exists:

    * a non-SMB scheme (sftp/ftp/nfs/webdav/file) — UNC is SMB-only;
    * a URL that carries an explicit ``:port`` — UNC has no port syntax;
    * a hostless URL.

    Embedded credentials (``smb://user:pass@host/…``) are **dropped** — a UNC
    string never carries them. An IPv6-literal host is rendered in Windows'
    ``dashed.ipv6-literal.net`` form. Path segments (incl. spaces) are preserved
    verbatim; nothing is percent-decoded (these are display/open paths)."""
    if not url or not url.lower().startswith("smb://"):
        return None
    rest = url[len("smb://") :]
    # Strip any userinfo (credentials) ahead of the first path separator.
    slash = rest.find("/")
    at = rest.find("@")
    if at != -1 and (slash == -1 or at < slash):
        rest = rest[at + 1 :]
        slash = rest.find("/")
    hostport = rest if slash == -1 else rest[:slash]
    pathpart = "" if slash == -1 else rest[slash + 1 :]
    if hostport.startswith("["):  # bracketed IPv6 literal, maybe with :port
        end = hostport.find("]")
        if end == -1:
            return None
        host6 = hostport[1:end]
        after = hostport[end + 1 :]
        if after.startswith(":"):  # explicit port — not representable in UNC
            return None
        host = host6.replace(":", "-") + ".ipv6-literal.net"
    else:
        if ":" in hostport:  # host:port — not representable in UNC
            return None
        host = hostport
    if not host:
        return None
    segs = [seg for seg in pathpart.split("/") if seg]
    body = "\\".join(segs)
    return f"\\\\{host}\\{body}" if body else f"\\\\{host}"


def _derive_url_from_unc(unc: str) -> str | None:
    """``\\host\\share\\sub`` → ``smb://host/share/sub`` (SMB URL).

    ``None`` when the string is not a ``\\\\``-anchored UNC or carries no host.
    A Windows ``dashed.ipv6-literal.net`` host is restored to a bracketed IPv6
    literal. Segments (incl. spaces) are preserved verbatim."""
    if not unc or not unc.startswith("\\\\"):
        return None
    segs = [seg for seg in unc[2:].replace("\\", "/").split("/") if seg]
    if not segs:
        return None
    host = segs[0]
    if host.lower().endswith(".ipv6-literal.net"):
        host = "[" + host[: -len(".ipv6-literal.net")].replace("-", ":") + "]"
    body = "/".join(segs[1:])
    return f"smb://{host}/{body}" if body else f"smb://{host}"


def _location_from_prefix(prefix: str) -> ShareLocation:
    """Classify a single manual share prefix and fill in BOTH OS renderings.

    * UNC (``\\\\host\\share``) → ``unc`` = the prefix, ``url`` = derived SMB URL.
    * SMB URL (``smb://…``) → ``url`` = the prefix, ``unc`` = derived UNC.
    * any other URL scheme (sftp/ftp/nfs/webdav/file), a POSIX mount, or an
      unclassifiable prefix → ``url`` = the prefix, ``unc`` = ``None`` (no UNC form)."""
    kind = classify_prefix(prefix)
    if kind == "unc":
        return ShareLocation(url=_derive_url_from_unc(prefix), unc=prefix)
    if kind == "url" and prefix.lower().startswith("smb://"):
        return ShareLocation(url=prefix, unc=_derive_unc_from_url(prefix))
    return ShareLocation(url=prefix, unc=None)


def effective_library_share_location(
    share_prefix: str | None, root_path: str
) -> tuple[ShareLocation, str]:
    """Both-format sibling of :func:`effective_library_share`.

    Returns ``(ShareLocation, source)`` (source values identical to
    :func:`effective_library_share`: ``manual`` / ``mount-map`` / ``none``). A
    manual prefix is classified and its counterpart format derived; a mount-map
    hit carries BOTH the deploy's ``share_url`` and ``unc`` verbatim; ``none``
    yields an empty location. ``share_prefix_effective`` (the URL-ish string the
    existing consumers read) is intentionally left to
    :func:`effective_library_share` — this only ADDS the UNC counterpart."""
    if share_prefix:
        return _location_from_prefix(share_prefix), "manual"
    hint = resolve(root_path)
    if hint is not None:
        return ShareLocation(url=hint.share_url, unc=hint.unc), "mount-map"
    return ShareLocation(), "none"


def item_share_location(
    share_prefix: str | None, item_path: str, rel_path: str
) -> ShareLocation:
    """Both-format sibling of :func:`item_share_url` for a SINGLE item.

    Manual prefix wins (joined with ``rel_path``, then classified so the missing
    OS format is derived); else the deploy mount map resolves the absolute
    container path (carrying its ``share_url`` + ``unc``). Empty location when
    neither applies. Consumers keep reading the URL form from
    :func:`item_share_url` (backward-compatible ``share_url`` column); this
    function's ``.unc`` supplies the new Windows counterpart alongside it (for a
    manual SMB URL / mount-map SMB entry it is the derived/served UNC; for a
    non-SMB scheme or POSIX mount it is ``None``)."""
    if share_prefix:
        segs = [s for s in rel_path.replace("\\", "/").split("/") if s]
        joined = _join_share(share_prefix, segs)
        return _location_from_prefix(joined)
    hint = resolve(item_path)
    if hint is not None:
        return ShareLocation(url=hint.share_url, unc=hint.unc)
    return ShareLocation()


# --------------------------------------------------------------------------- #
# P10-T12: central agent share-mapping resolution (both-format, UI-T15).       #
#                                                                              #
# The per-AGENT sibling of the deploy-sourced, library-scoped resolution above. #
# ``agent_share_maps`` rows (admin-defined) are loaded from the DB by the API   #
# layer and passed here as pure :class:`filearr.transfers.ShareMapping` values; #
# selection reuses the ONE canonical longest-prefix selector                    #
# (:func:`filearr.transfers._select_mapping`, R4), and the missing OS format is  #
# derived with the same UI-T15 helpers as the manual/library case, so an agent   #
# mapping, a library ``share_prefix``, and the browser all agree.                #
#                                                                              #
# WIRE-UP SEAM (P10-T2, not wired here): items are not agent-owned until         #
# ``items.source_agent_id`` gains its ``agents`` FK (P10-T2). Once it does, the   #
# item-display path becomes: if ``item.source_agent_id`` is set, first consult   #
# the agent's replicated ``ShareHint`` (P10-T11, source of display truth ONLY    #
# when present — R1); otherwise call :func:`resolve_for_agent` with the loaded    #
# ``agent_share_maps`` and ``item.source_agent_id`` + the item's agent-local      #
# path. A centrally-scanned item (``source_agent_id IS NULL``) keeps using        #
# :func:`item_share_location` (library ``share_prefix`` / deploy mount map). This #
# function + its tests are that seam; only the item-level dispatch awaits P10-T2. #
# --------------------------------------------------------------------------- #

from filearr.transfers import ShareMapping, _select_mapping  # noqa: E402


def resolve_for_agent(
    mappings: list[ShareMapping], agent_id: str | None, local_path: str
) -> ShareLocation | None:
    """Resolve an agent-local ``local_path`` (as agent ``agent_id`` sees it) to a
    both-format :class:`ShareLocation` (URL + Windows UNC) via the most specific
    covering :class:`filearr.transfers.ShareMapping`, or ``None`` when none
    covers it (caller then renders no network-open link — no fabricated location).

    Selection is the single canonical longest-``local_prefix``-wins resolver
    (:func:`filearr.transfers._select_mapping`): an agent-scoped mapping outranks
    a global (``agent_id=None`` = any agent) one of equal prefix length, and a
    concrete agent's paths never resolve against another agent's mapping. The
    winning ``share_prefix`` is joined with the leftover segments (native
    separator); the counterpart OS format is taken from the mapping's explicit
    ``unc`` when present (joined the same way), else DERIVED from the joined URL
    with the UI-T15 helper (SMB only; a non-SMB scheme / POSIX mount yields
    ``unc=None``). Pure: no I/O when ``mappings`` is supplied."""
    sel = _select_mapping(mappings, agent_id, local_path)
    if sel is None:
        return None
    best, remainder = sel
    url = _join_share(best.share_prefix, remainder)
    if best.unc:
        return ShareLocation(url=url, unc=_join_share(best.unc, remainder))
    # No explicit UNC: derive the missing OS counterpart from the joined URL
    # exactly as the manual/library case does (UI-T15), so both formats agree.
    return _location_from_prefix(url)
