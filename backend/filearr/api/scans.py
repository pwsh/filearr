"""Scan status + native SSE progress stream.

The events endpoint uses FastAPI's native SSE support
(`fastapi.sse.EventSourceResponse`, available >=0.135). The path operation is an
async generator yielding `ServerSentEvent` objects; the framework's routing
layer decouples the generator from a keepalive timer (so idle proxies don't kill
the stream), inserts `: ping` comment lines on timeout, and tears the generator
down cleanly on client disconnect — no leaked tasks.

Progress is published by the scan task into `ScanRun.stats` (batched commits
every 250 files). This endpoint polls that row on a short interval and emits a
`progress` event whenever the observed status/stats change, then a terminal
`done` event and returns once the scan reaches
finished/failed/cancelled/stopped.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit
from filearr.config import get_settings
from filearr.db import SessionLocal, get_session
from filearr.models import ScanRun
from filearr.schemas import ScanOut
from filearr.security import _verify_credentials, require_scope

router = APIRouter()

# Terminal scan states — the stream sends a final `done` event then closes.
# `stopped` (UI-T13 graceful stop) is terminal; `stopping` is the transient
# request marker the scan task is still draining, so it is NOT terminal (the
# stream keeps ticking through the stop wrap-up until the run flips to stopped).
_TERMINAL = ("finished", "failed", "cancelled", "stopped")

# How often we re-read ScanRun.stats. This is a DB poll interval, NOT the SSE
# keepalive interval (keepalives are inserted by the framework on top of the
# generator's own output when it goes idle). Kept short so the UI ticks live.
_POLL_INTERVAL = 1.0

_bearer = HTTPBearer(auto_error=False)


@router.get("", response_model=list[ScanOut], dependencies=[Depends(require_scope("read"))])
async def list_scans(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(ScanRun).order_by(ScanRun.started_at.desc()).limit(50))
    return result.scalars().all()


@router.post("/{scan_id}/cancel", dependencies=[Depends(require_scope("write"))])
async def cancel_scan(scan_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    """Mark a scan cancelled. The running task checks this flag between batches
    and aborts; orphaned rows (dead worker) are simply cleared by this."""
    run = (
        await session.execute(select(ScanRun).where(ScanRun.id == scan_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Scan not found")
    if run.status != "running":
        raise HTTPException(409, f"Scan is '{run.status}', not running")
    run.status = "cancelled"
    run.finished_at = datetime.now(UTC)
    await session.commit()
    return {"status": "cancelled"}


@router.post("/{scan_id}/stop", dependencies=[Depends(require_scope("write"))])
async def stop_scan(scan_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    """Gracefully stop a running scan, KEEPING everything scanned so far.

    Unlike ``/cancel`` (a hard abort), a stop lets the current batch finish, then
    the scan runs its post-walk wrap-up RESTRICTED to what it already saw: move
    detection and tombstoning are BOTH skipped (a partial walk cannot tell a
    genuinely-deleted file from one it simply hadn't reached yet, so tombstoning
    would falsely mark still-present files missing — invariant 4), while sidecar
    association + reindex still run (idempotent recompute over the current rows).
    The run ends terminal as ``stopped`` and its job SUCCEEDS (never failed), so
    locks free exactly like a completed scan.

    This leaves the library partially rescanned; that is safe and self-correcting:
    the next scheduled or manual scan is an ordinary scan whose full diff naturally
    processes whatever this run didn't reach (and tombstones anything truly gone).

    Signalling rides the same channel as cancel: this flips the ScanRun to the
    transient ``stopping`` state, which the running task observes between batches.
    Idempotent — a scan already ``stopping``/``stopped`` returns 200; a scan that
    is not running (finished/failed/cancelled) returns 409; unknown id 404.
    """
    run = (
        await session.execute(select(ScanRun).where(ScanRun.id == scan_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Scan not found")
    # Idempotent: a stop already in flight (or already landed) is a success, not a
    # conflict — the caller's intent (this run should stop) is already satisfied.
    if run.status in ("stopping", "stopped"):
        return {"status": run.status}
    if run.status != "running":
        raise HTTPException(409, f"Scan is '{run.status}', not running")
    # FIX-15 stop-path hardening: a 'stopping' marker is ONLY ever observed by a
    # LIVE scan worker between batches. If no worker is currently draining this
    # (library, scope) -- e.g. the operator clicked Stop on a scan whose worker had
    # already died and left the ScanRun 'running' -- setting 'stopping' would never
    # converge (the reaper only fails a *stalled doing* job's run; a gone job is
    # invisible to it). So probe for a live scan job: present -> set 'stopping' and
    # let the worker finish gracefully (today's behaviour); absent -> finalize to
    # terminal 'stopped' now, honoring the stop intent in one step. Any error
    # reaching the queue falls SAFE to the graceful path (never mark a possibly-live
    # scan stopped) -- the maintenance reconciler is the backstop either way.
    from filearr.worker import proc_app, scan_job_active

    active: bool | None = True
    try:
        async with proc_app.open_async():
            active = await scan_job_active(str(run.library_id), run.rel_path)
    except Exception:  # noqa: BLE001 - fall safe to the graceful (stopping) path
        active = True
    # Only a POSITIVE "no live worker" (False) finalizes immediately; True or
    # None (unknown / no procrastinate schema) keeps the legacy graceful path.
    if active is not False:
        # Transient marker only: do NOT set finished_at — the run is still draining
        # its last batch + wrap-up. The scan task sets terminal 'stopped'.
        run.status = "stopping"
        await session.commit()
        return {"status": "stopping"}
    # Orphaned run (no live worker): finalize directly so it never sticks.
    run.status = "stopped"
    run.finished_at = datetime.now(UTC)
    run.stats = {
        **(run.stats or {}),
        "stopped": True,
        "reconcile_note": "stop requested with no live scan worker; finalized "
        "as stopped by the stop endpoint (FIX-15)",
    }
    await session.commit()
    return {"status": "stopped"}


@router.post("/{scan_id}/force-clear", dependencies=[Depends(require_scope("admin"))])
async def force_clear_scan(
    scan_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Force a stuck ScanRun terminal (FIX-15, admin scope, audited).

    The manual escape hatch for a run wedged non-terminal (``stopping`` that never
    got observed, or ``running`` orphaned by a dead worker) that the automatic
    reconciler has not yet swept. Semantics:

      * unknown id -> 404.
      * already terminal (finished/failed/cancelled/stopped) -> 409 (nothing to
        clear; the run is done).
      * truly active -- a LIVE worker is currently draining this (library, scope)
        -> 409 'still active; use stop' (never yank a live scan out from under its
        worker; the graceful ``/stop`` is the right tool).
      * otherwise (non-terminal, no live worker) -> finalized to terminal
        ``stopped`` with a force-clear note, and a ``security_events`` audit row.

    Auditing rides the append-only security log (own transaction, never raises).
    """
    run = (
        await session.execute(select(ScanRun).where(ScanRun.id == scan_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "Scan not found")
    if run.status in _TERMINAL:
        raise HTTPException(
            409, f"Scan is already terminal ('{run.status}'); nothing to clear"
        )
    # Refuse only a genuinely-active run (live worker draining it). Fail SAFE to
    # 'not active' when the queue is unreachable: this is an explicit operator
    # repair action, so an unreachable/queue-less DB must not block the clear.
    from filearr.worker import proc_app, scan_job_active

    active: bool | None = False
    try:
        async with proc_app.open_async():
            active = await scan_job_active(str(run.library_id), run.rel_path)
    except Exception:  # noqa: BLE001 - fail open: allow the manual repair
        active = False
    if active is True:
        raise HTTPException(
            409,
            "Scan is still active (a live worker is processing it); use stop instead",
        )
    prev = run.status
    run.status = "stopped"
    run.finished_at = datetime.now(UTC)
    run.stats = {
        **(run.stats or {}),
        "stopped": True,
        "force_cleared": True,
        "reconcile_note": f"force-cleared from '{prev}' by operator (FIX-15)",
    }
    await session.commit()
    await audit.emit(
        audit.SCAN_FORCE_CLEARED,
        request=request,
        details={
            "scan_id": str(run.id),
            "library_id": str(run.library_id),
            "rel_path": run.rel_path,
            "previous_status": prev,
        },
    )
    return {"status": "stopped", "previous_status": prev}


async def _require_events_scope(
    request: Request,
    api_key: str | None = None,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Auth for the SSE stream.

    `EventSource` cannot set request headers, so browsers can't send a Bearer
    token on the stream. We therefore accept the API key either via the normal
    `Authorization: Bearer` header (non-browser clients) OR via a `?api_key=`
    query parameter — but ONLY on this read-only events endpoint. The key is
    verified through the same constant-time hash lookup + scope check as every
    other endpoint (`security._verify_credentials`); it is never logged or
    echoed back. When auth is disabled (trusted-LAN dev) this is a no-op, matching
    the rest of the API.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return
    token = creds.credentials if creds is not None else api_key
    if not token:
        raise HTTPException(401, "Missing bearer token or api_key")
    await _verify_credentials(token, "read", session, request)


def _snapshot(run: ScanRun) -> dict:
    """Build the wire payload for a progress tick.

    Additive to the stored `stats`: we fold in `status` and derive a live
    `rate` (files/s) and `elapsed` from timestamps so the UI can show a rate
    without the scan task having to persist it. Never mutates the DB row.
    """
    stats = dict(run.stats or {})
    started = run.started_at
    end = run.finished_at or datetime.now(UTC)
    elapsed = max((end - started).total_seconds(), 0.0) if started else 0.0
    seen = stats.get("seen", 0) or 0
    rate = round(seen / elapsed, 1) if elapsed > 0 else 0.0
    return {
        "scan_id": str(run.id),
        "library_id": str(run.library_id),
        "status": run.status,
        "elapsed": round(elapsed, 1),
        "rate": rate,
        **stats,
    }


@router.get("/{scan_id}/events", response_class=EventSourceResponse)
async def scan_events(
    scan_id: uuid.UUID,
    _: None = Depends(_require_events_scope),
):
    """Native SSE stream of scan progress.

    The endpoint is an async generator yielding `ServerSentEvent`; declaring
    `response_class=EventSourceResponse` tells FastAPI's routing layer to stream
    it as `text/event-stream`, insert keepalive `: ping` comments when idle (so
    intermediary proxies won't drop the connection), and finalize the generator
    cleanly on client disconnect — no leaked tasks.

    Events:
      * `progress` — a `_snapshot` payload; emitted on first read and whenever
        status or stats change.
      * `done`     — final `_snapshot` once the scan is terminal; stream closes.
      * `error`    — `{"detail": ...}` if the scan id is unknown.
    """
    last_serialized: str | None = None
    while True:
        # Short-lived session per poll so we never pin a pooled connection for
        # the whole stream lifetime.
        async with SessionLocal() as session:
            run = (
                await session.execute(select(ScanRun).where(ScanRun.id == scan_id))
            ).scalar_one_or_none()

        if run is None:
            yield ServerSentEvent(event="error", data={"detail": "scan not found"})
            return

        snap = _snapshot(run)
        # Change-detection uses ONLY the meaningful fields (status + persisted
        # stats), NOT the derived `elapsed`/`rate` — those tick every poll and
        # would otherwise emit a progress event each interval, starving the
        # framework's idle keepalive. When the scan is genuinely idle the stream
        # goes quiet and `: ping` comments keep the connection alive.
        key = json.dumps({"status": run.status, "stats": run.stats}, sort_keys=True)

        if run.status in _TERMINAL:
            # T11: a FAILED scan emits a dedicated `error` event carrying the
            # retained (sanitized) message from stats.error before the terminal
            # `done`, so the UI can surface *why* it failed -- not just that it
            # did. The error string was sanitized at store time (scan crash
            # handler), so it is safe to forward verbatim here.
            if run.status == "failed" and (snap.get("error")):
                yield ServerSentEvent(
                    event="error",
                    data={"scan_id": str(run.id), "detail": snap["error"]},
                )
            # Always emit the terminal state (even if unchanged) so a client that
            # connected late still gets a clean close.
            yield ServerSentEvent(event="done", data=snap)
            return

        if key != last_serialized:
            last_serialized = key
            yield ServerSentEvent(event="progress", data=snap)

        await asyncio.sleep(_POLL_INTERVAL)
