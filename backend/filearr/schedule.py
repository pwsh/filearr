"""Scheduling + watch-mode support (T5).

Two concerns live here, both pure-ish helpers kept out of the worker/API modules
so they are unit-testable without a running Postgres or Procrastinate:

  * ``due_occurrence`` / ``cron_is_due`` / ``validate_cron`` — evaluate a library's
    ``scan_cron`` for a given tick using **cronsim** (croniter is EOL). A single
    static periodic task in ``worker.py`` walks enabled libraries every minute and
    calls ``due_occurrence`` (FIX-8: once-per-occurrence, state in
    ``last_cron_fired_at``); ``cron_is_due`` is the older stateless exact-minute
    predicate, kept for the API's validation/preview paths. There is no dynamic
    periodic registration, so a schedule change takes effect on the next tick
    without a worker restart.

  * network-filesystem detection for the watch-mode guard. inotify (watchfiles'
    backend on Linux) is unreliable over SMB/NFS/FUSE, so watch mode is refused
    for library roots that live on a network mount. Detection parses
    ``/proc/self/mountinfo`` and resolves the *containing* mount for a path
    (longest mount-point prefix wins), then classifies its fstype.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from cronsim import CronSim, CronSimError


class InvalidCronError(ValueError):
    """Raised when a scan_cron expression cannot be parsed by cronsim."""


def validate_cron(expr: str) -> None:
    """Parse ``expr`` with cronsim; raise :class:`InvalidCronError` if invalid.

    An empty/whitespace-only expression is rejected here — callers treat
    ``None``/empty as "scheduling disabled" and must not pass it in.
    """
    if not expr or not expr.strip():
        raise InvalidCronError("empty cron expression")
    try:
        # CronSim validates at construction; the reference datetime is arbitrary.
        CronSim(expr.strip(), datetime(2000, 1, 1))
    except CronSimError as exc:
        raise InvalidCronError(str(exc)) from exc


def cron_is_due(expr: str, tick: datetime) -> bool:
    """True when ``expr`` fires at ``tick`` (minute granularity).

    We floor ``tick`` to the minute and ask cronsim for the first match at or
    after ``tick - 1 minute``; the expression is due iff that match equals the
    floored tick. This makes evaluation exact and idempotent for a given minute,
    so a duplicate/late periodic tick for the same minute yields the same answer.
    Invalid expressions are treated as "not due" (never raise into the tick loop);
    validation is enforced at the API layer on write.
    """
    if not expr or not expr.strip():
        return False
    floored = tick.replace(second=0, microsecond=0)
    try:
        nxt = next(CronSim(expr.strip(), floored - timedelta(minutes=1)))
    except (CronSimError, StopIteration):
        return False
    # cronsim yields tz-aware/naive matching the input; compare on naive minute.
    return nxt.replace(tzinfo=None) == floored.replace(tzinfo=None)


# --- FIX-8 (scan-scheduling storm): once-per-occurrence firing --------------
# ``cron_is_due`` answers "is THIS exact minute an occurrence" and is what the
# scheduler used before FIX-8. It is stateless, so two ticks in the SAME minute
# both say "due" once the first-deferred job has left the ``todo`` state (the
# partial queueing_lock only covers ``todo``) -- a double-fire. It also cannot
# express "the daily 04:00 run was missed while the worker was down; fire it
# once on recovery" without RE-firing every subsequent tick until it succeeds
# (the storm the live box hit).
#
# ``due_occurrence`` is the stateful replacement: given the library's persisted
# ``last_cron_fired_at`` it returns the LATEST occurrence at/before ``tick`` that
# is strictly newer than ``last_fired`` -- i.e. "an occurrence is due that we
# have not yet consumed" -- or ``None``. The caller records the returned instant
# back into ``last_cron_fired_at`` in the same commit as the enqueue, so each
# occurrence fires AT MOST ONCE regardless of how many ticks observe it. Missed
# occurrences are NEVER backfilled: only the single latest one fires (a week of
# downtime yields one catch-up scan, not one per missed slot).
DEFAULT_MAX_CATCHUP_MINUTES = 2880  # 48h: covers hourly/daily; bounds iteration


def due_occurrence(
    expr: str,
    tick: datetime,
    last_fired: datetime | None = None,
    *,
    max_catchup_minutes: int = DEFAULT_MAX_CATCHUP_MINUTES,
) -> datetime | None:
    """Latest un-consumed cron occurrence at/before ``tick``, or ``None``.

    Returns a tz-aware UTC ``datetime`` (the occurrence instant, minute-floored)
    to store back into ``last_cron_fired_at``; ``None`` when nothing new is due.

    Semantics (all minute-granular, evaluated in UTC to match the stored,
    fixed-UTC schedule):

      * ``last_fired`` is the exclusive lower bound -- an occurrence equal to or
        older than it is already consumed and never re-fires.
      * When ``last_fired`` is ``None`` (a schedule that has never fired -- newly
        set, or a fresh library) ONLY the current tick minute is considered, so
        setting a daily 04:00 schedule at 15:00 does NOT immediately fire a
        catch-up scan. This matches the pre-FIX-8 ``cron_is_due`` first-fire.
      * Multiple occurrences between ``last_fired`` and ``tick`` collapse to the
        single LATEST (no backfill).
      * ``max_catchup_minutes`` caps how far back we look (and thus bounds the
        cronsim iteration for a very frequent expression after a long outage);
        an occurrence older than the cap is not caught up.

    Invalid/empty expressions return ``None`` (never raise into the tick loop);
    validation is enforced at the API boundary on write.
    """
    if not expr or not expr.strip():
        return None
    floored = tick.replace(second=0, microsecond=0, tzinfo=None)
    cap_lower = floored - timedelta(minutes=max(1, max_catchup_minutes))
    if last_fired is None:
        lf = None
        lower = floored - timedelta(minutes=1)
    else:
        # Normalise to naive UTC minute for comparison with cronsim output.
        if last_fired.tzinfo is not None:
            lf = last_fired.astimezone(UTC).replace(tzinfo=None)
        else:
            lf = last_fired
        lf = lf.replace(second=0, microsecond=0)
        lower = max(lf, cap_lower)
    try:
        # CronSim(expr, X) yields occurrences STRICTLY AFTER X (verified against
        # the installed cronsim: starting exactly on an occurrence yields the
        # NEXT one). Iterate up to and including ``floored`` and keep the last.
        latest: datetime | None = None
        for occ in CronSim(expr.strip(), lower):
            occ_naive = occ.replace(tzinfo=None)
            if occ_naive > floored:
                break
            latest = occ_naive
    except (CronSimError, StopIteration, ValueError):
        return None
    if latest is None:
        return None
    if lf is not None and latest <= lf:
        return None
    return latest.replace(tzinfo=UTC)


def next_occurrence(expr: str, now: datetime) -> datetime | None:
    """The next cron occurrence strictly AFTER ``now`` (tz-aware UTC), or ``None``.

    The forward-looking counterpart of :func:`due_occurrence` (which finds the
    latest PAST un-consumed occurrence). Used by the Jobs dashboard's "upcoming"
    projection to show when a library/hot-folder/report schedule will next fire.
    Reuses the SAME cronsim engine the scheduler evaluates, so a schedule that is
    valid for firing shows a consistent next time. Minute-granular (the scheduler
    tick is minutely); ``now`` is floored to the minute so an occurrence at the
    current minute is treated as already-in-progress and the NEXT one is returned.

    Invalid/empty expressions return ``None`` (never raise into a UI projection).
    """
    if not expr or not expr.strip():
        return None
    floored = now.astimezone(UTC).replace(second=0, microsecond=0, tzinfo=None) \
        if now.tzinfo is not None else now.replace(second=0, microsecond=0)
    try:
        nxt = next(CronSim(expr.strip(), floored))
    except (CronSimError, StopIteration, ValueError):
        return None
    return nxt.replace(tzinfo=UTC)


# --- network-filesystem detection (watch-mode guard) -----------------------

# fstypes where inotify is unreliable or unsupported. Matched case-insensitively
# against the mountinfo fstype field; ``fuse.*`` backends (rclone/sshfs/etc.) are
# matched by the ``fuse`` prefix so any FUSE remote is refused.
_NETWORK_FSTYPES = frozenset({
    "cifs", "smb", "smbfs", "smb3",
    "nfs", "nfs4", "nfsd",
    "afs", "9p",
    "fuseblk",  # ntfs-3g & friends are local, but see prefix handling below
})
_NETWORK_FUSE_BACKENDS = frozenset({
    "rclone", "sshfs", "davfs", "webdav", "smbnetfs", "s3fs", "gcsfuse",
    "cifs", "nfs", "glusterfs", "ceph", "mfs", "moosefs",
})
# Explicitly-local FUSE backends that DO support inotify well enough to allow.
_LOCAL_FUSE_BACKENDS = frozenset({"ext4", "ntfs", "ntfs-3g", "exfat", "vfat"})


def _classify_fstype(fstype: str) -> bool:
    """Return True if ``fstype`` (mountinfo field) denotes a network filesystem."""
    fs = fstype.lower()
    if fs in _NETWORK_FSTYPES and fs != "fuseblk":
        return True
    if fs == "fuseblk":
        # block-backed FUSE (ntfs-3g/exfat via fuse) — local.
        return False
    if fs.startswith("fuse."):
        backend = fs.split(".", 1)[1]
        if backend in _LOCAL_FUSE_BACKENDS:
            return False
        # Unknown/remote FUSE backend (rclone, sshfs, davfs, ...) -> treat as
        # network. Being conservative here is correct: a false "network" only
        # refuses watch mode (scheduled/manual scans still work), whereas a false
        # "local" would silently enable an unreliable watcher.
        return backend in _NETWORK_FUSE_BACKENDS or backend not in _LOCAL_FUSE_BACKENDS
    if fs == "fuse":  # bare "fuse" with no backend subtype — unknown, refuse.
        return True
    return False


def _parse_mountinfo(text: str) -> list[tuple[str, str]]:
    """Parse ``/proc/self/mountinfo`` into ``[(mount_point, fstype), ...]``.

    mountinfo line layout (man 5 proc):
        36 35 98:0 /mnt1 /mnt2 rw,noatime ... - ext3 /dev/root rw,errors=continue
    The fstype follows the ``-`` separator; the mount point is field index 4.
    Octal escapes (\\040 spaces etc.) in the mount point are decoded.
    """
    mounts: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        try:
            sep = parts.index("-")
        except ValueError:
            continue
        if len(parts) < 5 or sep + 1 >= len(parts):
            continue
        mount_point = _unescape_mount(parts[4])
        fstype = parts[sep + 1]
        mounts.append((mount_point, fstype))
    return mounts


def _unescape_mount(field: str) -> str:
    """Decode mountinfo octal escapes (space=\\040, tab=\\011, newline=\\012, \\=\\134)."""
    if "\\" not in field:
        return field
    out: list[str] = []
    i = 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) + 1 and field[i + 1 : i + 4].isdigit():
            try:
                out.append(chr(int(field[i + 1 : i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(field[i])
        i += 1
    return "".join(out)


def _containing_fstype(path: str, mounts: list[tuple[str, str]]) -> str | None:
    """fstype of the mount whose mount point is the longest prefix of ``path``."""
    try:
        target = os.path.realpath(path)
    except OSError:
        target = os.path.abspath(path)
    best: tuple[int, str] | None = None
    for mp, fstype in mounts:
        if target == mp or target.startswith(mp.rstrip("/") + "/") or mp == "/":
            score = len(mp.rstrip("/")) or 1
            if best is None or score > best[0]:
                best = (score, fstype)
    return best[1] if best else None


def is_network_path(path: str, mountinfo: str | None = None) -> bool:
    """True if ``path`` lives on a network/remote filesystem (SMB/NFS/FUSE remote).

    ``mountinfo`` may be supplied (test injection); otherwise
    ``/proc/self/mountinfo`` is read. If mountinfo is unavailable (non-Linux, or
    unreadable), we fail SAFE and treat the path as network so watch mode is not
    enabled on an unverifiable filesystem.
    """
    if mountinfo is None:
        try:
            with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
                mountinfo = fh.read()
        except OSError:
            return True  # cannot verify -> refuse watch mode (fail safe)
    fstype = _containing_fstype(path, _parse_mountinfo(mountinfo))
    if fstype is None:
        return True  # no matching mount -> unverifiable -> refuse
    return _classify_fstype(fstype)
