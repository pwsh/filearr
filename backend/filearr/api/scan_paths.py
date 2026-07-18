"""P2-T6 scan_paths CRUD: per-subfolder scan cadence / watch overrides.

Routes are mounted under ``/api/v1/libraries/{library_id}/scan-paths`` (the same
``/libraries`` prefix the library routes use). A ``scan_paths`` row is a pure
override on a subtree of a library: ``scan_cron``/``watch_mode`` NULL means
"inherit the library's". The scheduler (worker tick) and the watch supervisor
pick up new/edited rows on their next iteration -- no worker restart.

Security note (priority: security > integrity > ...): ``rel_path`` is joined to
the library root and walked, so it is a path-traversal surface. Every write
normalises it and rejects ``..``/absolute/NUL/empty-segment inputs BEFORE it can
reach the filesystem. ``watch_mode`` is refused for a path that resolves onto a
network mount, re-checked per resolved absolute path (the supervisor refuses
again defensively).
"""

import os
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.db import get_session
from filearr.models import Library, ScanPath
from filearr.schedule import InvalidCronError, is_network_path, validate_cron
from filearr.schemas import ScanPathIn, ScanPathOut, ScanPathUpdate
from filearr.security import require_scope

router = APIRouter()


def _normalize_rel_path(rel: str) -> str:
    """Normalise + validate a scan_paths ``rel_path`` (security-critical).

    Returns a posix-separated, slash-trimmed relative path ('' = library root).
    Raises HTTP 422 on any traversal / absolute / malformed input. Existence
    under the root is NOT required (a hot folder may be pre-created)."""
    if rel is None:
        return ""
    raw = rel.strip()
    if "\x00" in raw:
        raise HTTPException(422, "rel_path must not contain NUL")
    # Absolute paths (posix or Windows drive/UNC) are rejected outright: a
    # scan_paths rel_path is always relative to the library root.
    if raw.startswith("/") or raw.startswith("\\"):
        raise HTTPException(422, "rel_path must be relative, not absolute")
    if len(raw) >= 2 and raw[1] == ":":
        raise HTTPException(422, "rel_path must be relative, not a drive path")
    norm = raw.replace("\\", "/").strip("/")
    if norm == "":
        return ""
    segments = norm.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise HTTPException(
                422,
                "rel_path must be a normalized relative path "
                "(no empty, '.', or '..' segments)",
            )
    return norm


def _validate_scan_path(
    library: Library, scan_cron: str | None, watch_mode: bool | None, rel_path: str
) -> None:
    """Cron + watch-mode-network validation (HTTP 422 on violation)."""
    if scan_cron is not None and scan_cron.strip():
        try:
            validate_cron(scan_cron)
        except InvalidCronError as exc:
            raise HTTPException(422, f"invalid scan_cron: {exc}") from exc
    if watch_mode:
        abs_path = os.path.join(library.root_path, rel_path) if rel_path else library.root_path
        if is_network_path(abs_path):
            raise HTTPException(
                422,
                "watch_mode requires a local filesystem path; "
                f"{abs_path!r} resolves onto a network mount (SMB/NFS/FUSE) where "
                "inotify is unreliable. Use scan_cron for network subfolders.",
            )


async def _get_library(library_id: uuid.UUID, session: AsyncSession) -> Library:
    library = (
        await session.execute(select(Library).where(Library.id == library_id))
    ).scalar_one_or_none()
    if library is None:
        raise HTTPException(404, "Library not found")
    return library


@router.get(
    "/{library_id}/scan-paths",
    response_model=list[ScanPathOut],
    dependencies=[Depends(require_scope("read"))],
)
async def list_scan_paths(
    library_id: uuid.UUID, session: AsyncSession = Depends(get_session)
):
    await _get_library(library_id, session)
    rows = (
        await session.execute(
            select(ScanPath)
            .where(ScanPath.library_id == library_id)
            .order_by(ScanPath.rel_path)
        )
    ).scalars().all()
    return rows


@router.post(
    "/{library_id}/scan-paths",
    response_model=ScanPathOut,
    status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_scan_path(
    library_id: uuid.UUID,
    body: ScanPathIn,
    session: AsyncSession = Depends(get_session),
):
    library = await _get_library(library_id, session)
    rel_path = _normalize_rel_path(body.rel_path)
    scan_cron = body.scan_cron.strip() if body.scan_cron and body.scan_cron.strip() else None
    _validate_scan_path(library, scan_cron, body.watch_mode, rel_path)
    row = ScanPath(
        library_id=library_id,
        rel_path=rel_path,
        scan_cron=scan_cron,
        watch_mode=body.watch_mode,
        enabled=body.enabled,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            409, f"a scan_path already exists for rel_path {rel_path!r} in this library"
        ) from exc
    await session.refresh(row)
    return row


@router.patch(
    "/{library_id}/scan-paths/{scan_path_id}",
    response_model=ScanPathOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def update_scan_path(
    library_id: uuid.UUID,
    scan_path_id: uuid.UUID,
    body: ScanPathUpdate,
    session: AsyncSession = Depends(get_session),
):
    library = await _get_library(library_id, session)
    row = (
        await session.execute(
            select(ScanPath).where(
                ScanPath.id == scan_path_id, ScanPath.library_id == library_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "scan_path not found")

    # model_fields_set discipline: distinguish "absent" (leave) from "explicit
    # null" (clear -> inherit) for the nullable override columns.
    dumped = body.model_dump()
    fields = {name: dumped[name] for name in body.model_fields_set}
    if "rel_path" in fields:
        fields["rel_path"] = _normalize_rel_path(fields["rel_path"] or "")
    if "scan_cron" in fields:
        cron = fields["scan_cron"]
        fields["scan_cron"] = cron.strip() if cron and cron.strip() else None

    eff_cron = fields.get("scan_cron", row.scan_cron)
    eff_watch = fields.get("watch_mode", row.watch_mode)
    eff_rel = fields.get("rel_path", row.rel_path)
    _validate_scan_path(library, eff_cron, eff_watch, eff_rel)

    for key, value in fields.items():
        setattr(row, key, value)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            409, f"a scan_path already exists for rel_path {eff_rel!r} in this library"
        ) from exc
    await session.refresh(row)
    return row


@router.delete(
    "/{library_id}/scan-paths/{scan_path_id}",
    status_code=204,
    dependencies=[Depends(require_scope("admin"))],
)
async def delete_scan_path(
    library_id: uuid.UUID,
    scan_path_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    await _get_library(library_id, session)
    row = (
        await session.execute(
            select(ScanPath).where(
                ScanPath.id == scan_path_id, ScanPath.library_id == library_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "scan_path not found")
    await session.delete(row)
    await session.commit()
