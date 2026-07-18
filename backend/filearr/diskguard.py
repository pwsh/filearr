"""FIX-11: filesystem-full guardrails + low-space policy (pure-ish core).

A LIVE INCIDENT motivated this module: unbounded thumbnail generation filled the
container filesystem (``/config`` on the Proxmox LXC holds the thumbnail cache,
the Postgres data dir AND the Meilisearch store in that single-volume deploy),
and Postgres crashed when it could no longer extend a file. The lesson is the
classic one: a *producer* must refuse to write BEFORE the disk fills, and an
operator must be told while there is still headroom to act.

This module is the dependency-light core — ``os.statvfs`` + a pure ``evaluate``
policy. It owns NO database, NO Procrastinate and NO FastAPI; those live in
``filearr.tasks.diskmon`` (the periodic monitor + emergency GC), the producer
call-sites (``tasks.thumbs`` / ``tasks.ocr_run`` / ``embed`` / ``api.items``) and
``api.system`` (the ``/system/disk`` endpoint). Two concerns:

  1. **Status / policy** — ``disk_status`` reads free/total for the filesystem
     holding a path; ``evaluate`` maps ``(free_bytes, total)`` to
     ``ok|warn|critical`` against BOTH an absolute-GB floor and a percent floor,
     **the more conservative (higher-severity) axis winning**. This dual floor is
     deliberate: a 5 GB floor is meaningful on a 200 GB volume but is already
     ~more~ than 10% of a 40 GB LXC rootfs, so neither axis alone is safe across
     the deployment sizes filearr runs on.

  2. **Producer guard** — ``guard_write`` is the fail-closed pre-write check every
     ``/config``/tmp writer calls. At ``critical`` it raises :class:`DiskGuardError`
     (message carries the ``disk_full_guard`` token so a failed job is
     unambiguous); at ``warn`` it logs once per path per hour and returns. It
     caches the ``statvfs`` result for ``disk_guard_cache_s`` seconds so a
     per-file producer loop pays one syscall every few seconds, not one per file.

Fail-open on error is a rule here: if ``statvfs`` itself fails (an unusual mount
state), the guard must NOT block writes — a monitoring feature must never become
a new outage. Only a *positively observed* critical free level fail-closes.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from dataclasses import dataclass, field

log = logging.getLogger("filearr.diskguard")

GB = 1024**3

OK = "ok"
WARN = "warn"
CRITICAL = "critical"
RECOVERED = "recovered"  # transient status the monitor emits on warn/critical -> ok

_ORDER = {OK: 0, WARN: 1, CRITICAL: 2}


def more_severe(a: str, b: str) -> str:
    """Return whichever of two statuses is the more severe (ties -> ``a``)."""
    return a if _ORDER[a] >= _ORDER[b] else b


class DiskGuardError(RuntimeError):
    """Fail-closed sentinel: a producer refused a write because its target
    filesystem is at the CRITICAL low-space floor.

    The message embeds the ``disk_full_guard`` token so a failed Procrastinate
    job (or a log line) is unambiguously attributable to the guard rather than a
    generic OSError. ``disk`` carries the status dict for the caller/handler."""

    def __init__(self, path: str, disk: dict):
        self.path = path
        self.disk = disk
        super().__init__(
            f"disk_full_guard: refusing write to {path!r} — free "
            f"{disk.get('free', 0)} bytes ({disk.get('pct_free', 0.0):.1f}%), "
            f"status=critical ({disk.get('reason', '')})"
        )


# --- raw status ------------------------------------------------------------- #

def _resolve_target(path: str) -> str:
    """Nearest EXISTING ancestor of ``path`` (``statvfs`` needs a live node).

    A producer often checks a path before creating it (the thumbnail fanout dir,
    a temp file). Walking up to the first existing directory gives the correct
    filesystem for that path without requiring it to exist yet."""
    p = os.path.abspath(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:  # reached the root and it still "doesn't exist"
            break
        p = parent
    return p


def disk_status(path: str) -> dict:
    """Raw free/total for the filesystem holding ``path`` via ``os.statvfs``.

    ``free`` uses ``f_bavail`` (blocks available to a NON-root process — the
    honest headroom a worker actually has, since root-reserved blocks cannot be
    written by the app). Returns ``{total, free, used, pct_free, dev}``. Raises
    ``OSError`` only if even the resolved ancestor is unstatable (the caller
    fails OPEN on that)."""
    target = _resolve_target(path)
    st = os.statvfs(target)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    # "used" from the OS view (total minus ALL free incl. root-reserved) so the
    # dashboard's used+free≈total reads naturally; the policy still gates on the
    # non-root ``free`` above.
    used = max(0, total - st.f_bfree * st.f_frsize)
    pct_free = (free / total * 100.0) if total > 0 else 0.0
    dev = None
    try:
        dev = os.stat(target).st_dev
    except OSError:
        pass
    return {"total": total, "free": free, "used": used, "pct_free": pct_free, "dev": dev}


# --- policy ----------------------------------------------------------------- #

def evaluate(
    free_bytes: int,
    total: int,
    *,
    min_free_gb: float,
    warn_free_gb: float,
    crit_pct: float,
    warn_pct: float,
) -> tuple[str, str]:
    """Map ``(free_bytes, total)`` to ``(status, reason)`` against two floors.

    Absolute-GB axis and percent-of-total axis are evaluated INDEPENDENTLY and
    the more conservative (higher-severity) result wins — so ``critical`` fires
    when EITHER ``free < min_free_gb`` OR ``pct_free < crit_pct``. ``reason`` names
    the axis that drove the winning severity (GB axis preferred on a tie) so the
    alert/endpoint says exactly which floor tripped."""
    pct = (free_bytes / total * 100.0) if total > 0 else 0.0

    if free_bytes < min_free_gb * GB:
        gb_status = CRITICAL
    elif free_bytes < warn_free_gb * GB:
        gb_status = WARN
    else:
        gb_status = OK

    if pct < crit_pct:
        pct_status = CRITICAL
    elif pct < warn_pct:
        pct_status = WARN
    else:
        pct_status = OK

    status = more_severe(gb_status, pct_status)
    if status == OK:
        return OK, "ok"

    # Which axis set the winning severity? Prefer the GB axis when both match.
    free_gb = free_bytes / GB
    if gb_status == status:
        floor = min_free_gb if status == CRITICAL else warn_free_gb
        reason = f"free {free_gb:.2f}GB < {floor:g}GB floor"
    else:
        floor = crit_pct if status == CRITICAL else warn_pct
        reason = f"free {pct:.1f}% < {floor:g}% floor"
    return status, reason


def status_for_path(path: str, settings) -> dict:
    """Full status dict for ``path``: raw ``statvfs`` + policy verdict.

    Fails OPEN: an unstatable path returns an ``ok`` row flagged ``exists=False``
    so a broken watch entry never blocks a producer or spams a false alert."""
    try:
        raw = disk_status(path)
    except OSError as exc:
        log.debug("statvfs failed for %s: %s", path, exc)
        return {
            "path": path,
            "exists": False,
            "total": 0,
            "free": 0,
            "used": 0,
            "pct_free": 0.0,
            "dev": None,
            "status": OK,
            "reason": "unstatable",
        }
    status, reason = evaluate(
        raw["free"],
        raw["total"],
        min_free_gb=settings.disk_min_free_gb,
        warn_free_gb=settings.disk_warn_free_gb,
        crit_pct=settings.disk_crit_pct_free,
        warn_pct=settings.disk_warn_pct_free,
    )
    return {
        "path": path,
        "exists": os.path.exists(path),
        **raw,
        "status": status,
        "reason": reason,
    }


# --- watch-path resolution -------------------------------------------------- #

@dataclass
class WatchTarget:
    label: str
    path: str
    is_pg: bool = field(default=False)


def watch_targets(settings) -> list[WatchTarget]:
    """The monitored paths for this process.

    ``FILEARR_DISK_WATCH_PATHS`` (JSON list) overrides the derived defaults
    entirely. Otherwise: the thumbnail cache dir (the FIX-11 culprit), the tmp
    dir (OCR rasterisation + atomic-write staging), and — ONLY if visible to this
    process — the Postgres data dir (``FILEARR_DISK_PG_PATH``). In the compose
    stack Postgres runs in its own container/volume and is invisible here, so it
    is skipped by default; in the single-volume LXC deploy the thumbnail dir and
    the PG dir share one filesystem, so watching ``/config`` already covers PG."""
    if settings.disk_watch_paths:
        return [WatchTarget(p, p) for p in settings.disk_watch_paths]
    targets = [
        WatchTarget("thumbnails", os.path.join(settings.config_dir, "thumbnails")),
        WatchTarget("tmp", tempfile.gettempdir()),
    ]
    pg = getattr(settings, "disk_pg_path", None)
    if pg:
        targets.append(WatchTarget("postgres", pg, is_pg=True))
    return targets


def monitored_statuses(settings) -> list[dict]:
    """Status dict for every watch target (endpoint/monitor helper).

    Each dict gains ``label`` and ``is_pg`` from its :class:`WatchTarget`. No
    device de-duplication here — the endpoint shows every logical path; the
    monitor de-dupes alerts by device so co-located paths do not double-fire."""
    out = []
    for t in watch_targets(settings):
        st = status_for_path(t.path, settings)
        st["label"] = t.label
        st["is_pg"] = t.is_pg
        out.append(st)
    return out


def overall_status(statuses: list[dict]) -> str:
    """The single worst status across a list of status dicts (for a banner)."""
    worst = OK
    for st in statuses:
        worst = more_severe(worst, st.get("status", OK))
    return worst


def dedupe_by_device(statuses: list[dict]) -> list[dict]:
    """Collapse per-watch-path status dicts to one row per PHYSICAL device.

    Several watch roles frequently live on the SAME filesystem (the single-volume
    LXC deploy holds the thumbnail cache, tmp AND Postgres on one device), so the
    always-on disk indicator would otherwise show the identical free/total three
    times. Rows are grouped by ``st_dev`` (the ``dev`` field :func:`disk_status`
    carries): each group yields ONE merged row whose ``label`` joins the member
    roles ("thumbnails, tmp"), ``status`` is the worst of the group (so a low
    device still tints), ``path`` is the first member's path, and ``members``
    lists every ``{label, path}`` for a "which roles share this device" tooltip.

    A missing/zero ``dev`` (an unstatable/degraded path, or a non-POSIX host with
    no ``st_dev``) is treated as its OWN device so distinct such paths never merge
    on a shared falsy key. Input order is preserved (first-seen device first).

    NOTE: this is deliberately NOT applied to the low-space banner list, which is
    per watch-role (an operator wants to know WHICH role hit its floor). The
    device-dedupe alert collapse in ``tasks.diskmon`` is a separate concern.
    """
    order: list[tuple] = []
    groups: dict[tuple, list[dict]] = {}
    fallback = 0
    for st in statuses:
        dev = st.get("dev")
        if dev:  # truthy int st_dev -> real device identity
            key: tuple = ("dev", dev)
        else:  # missing/zero -> unique per-path key (never merged)
            key = ("path", fallback)
            fallback += 1
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(st)

    out: list[dict] = []
    for key in order:
        grp = groups[key]
        first = grp[0]
        worst = OK
        worst_member = first
        for st in grp:
            s = st.get("status", OK)
            if more_severe(s, worst) == s and s != worst:
                worst_member = st
            worst = more_severe(worst, s)
        out.append(
            {
                "label": ", ".join(s.get("label", s["path"]) for s in grp),
                "path": first["path"],
                "total": first["total"],
                "free": first["free"],
                "used": first["used"],
                "pct_free": first["pct_free"],
                "status": worst,
                "reason": worst_member.get("reason", ""),
                "is_pg": any(s.get("is_pg", False) for s in grp),
                "dev": first.get("dev"),
                "members": [
                    {"label": s.get("label", s["path"]), "path": s["path"]}
                    for s in grp
                ],
            }
        )
    return out


# --- producer guard (cached) ------------------------------------------------ #

_CACHE: dict[str, tuple[float, dict]] = {}
_WARN_LOGGED: dict[str, float] = {}


def clear_cache() -> None:
    """Drop the cached statvfs results (tests + a forced re-check)."""
    _CACHE.clear()
    _WARN_LOGGED.clear()


def cached_status_for_path(path: str, settings, *, clock=time.monotonic) -> dict:
    """``status_for_path`` memoised for ``disk_guard_cache_s`` seconds per path.

    A per-file producer loop (thumbnails over a big library) must not statvfs
    once per file; this collapses it to one syscall every few seconds while still
    reacting quickly when the disk starts filling."""
    now = clock()
    ttl = settings.disk_guard_cache_s
    hit = _CACHE.get(path)
    if hit is not None and (now - hit[0]) < ttl:
        return hit[1]
    st = status_for_path(path, settings)
    _CACHE[path] = (now, st)
    return st


def guard_write(path: str, settings, *, clock=time.monotonic) -> dict:
    """Fail-closed pre-write disk check for any producer writing under ``path``.

    * ``critical`` -> raise :class:`DiskGuardError` (the caller skips the write;
      a thumbnail job fails with the ``disk_full_guard`` token, respecting the
      FIX-9 no-retry discipline — the failure is terminal, never a hot retry
      loop).
    * ``warn`` -> log ONCE per path per hour and return (the write proceeds; the
      periodic monitor owns the operator-facing alert).
    * ``ok`` -> return.

    Returns the status dict so a caller can inspect/propagate it."""
    st = cached_status_for_path(path, settings, clock=clock)
    if st["status"] == CRITICAL:
        raise DiskGuardError(path, st)
    if st["status"] == WARN:
        now = clock()
        last = _WARN_LOGGED.get(path)
        if last is None or (now - last) > 3600:
            _WARN_LOGGED[path] = now
            log.warning(
                "low disk (warn) for %s: free=%d (%.1f%%) — %s",
                path,
                st["free"],
                st["pct_free"],
                st["reason"],
            )
    return st


def is_critical(path: str, settings, *, clock=time.monotonic) -> bool:
    """Cheap cached 'is this path at the critical floor?' (no raise)."""
    return cached_status_for_path(path, settings, clock=clock)["status"] == CRITICAL
