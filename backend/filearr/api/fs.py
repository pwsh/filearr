"""UI-T4: server-side folder browser for the add/edit-library root_path picker.

``GET /api/v1/fs/browse?path=`` lists *directories only*, rooted at an allowlist
(``FILEARR_BROWSE_ROOTS``, default ``/data``). This endpoint hands filesystem
paths to an admin over HTTP, so ``path`` is treated as hostile input:

  * every candidate is normalized AND symlink-resolved (``os.path.realpath``)
    and must stay at/under a resolved root -- ``..`` traversal and symlinks that
    point out of the allowlist are rejected (422) / silently skipped;
  * only directories are returned; unreadable entries are skipped silently;
  * results are capped and name-sorted so a huge directory can't blow up the
    response.

Admin scope only (require_scope("admin")).
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from filearr.config import get_settings
from filearr.security import require_scope

router = APIRouter()

# Hard cap on directory entries returned for one listing (name-sorted, truncated).
_MAX_ENTRIES = 500


def _resolved_roots() -> list[str]:
    """The configured browse roots, normalized + symlink-resolved to absolute real
    paths. Resolving here means the containment check compares real path to real
    path, so a symlinked root is handled consistently."""
    roots: list[str] = []
    seen: set[str] = set()
    for r in get_settings().browse_roots:
        real = os.path.realpath(r)
        if real not in seen:
            seen.add(real)
            roots.append(real)
    return roots


def _root_name(root: str) -> str:
    """Display name for a root entry: its basename, or the path itself for '/'."""
    return os.path.basename(root.rstrip("/")) or root


def _containing_root(real_path: str, roots: list[str]) -> str | None:
    """Return the root that contains ``real_path`` (equal or a prefix at a path
    boundary), or None if it is outside every root. Both arguments must already be
    absolute real paths. The ``+ os.sep`` guard prevents ``/data-secret`` from
    matching root ``/data``."""
    for root in roots:
        if real_path == root or real_path.startswith(root + os.sep):
            return root
    return None


def _browse(path: str) -> dict:
    """Synchronous listing implementation. Runs in a threadpool (see the route)
    so its blocking filesystem calls never stall the event loop, and so the
    hostile-path hardening lives in one place. Raises HTTPException on rejection."""
    roots = _resolved_roots()

    # No path (or blank) -> present the roots themselves as the top level.
    if not path or not path.strip():
        return {
            "path": "",
            "parent": None,
            "roots": roots,
            "dirs": [{"name": _root_name(r), "path": r} for r in roots],
        }

    # Normalize + resolve symlinks, THEN enforce the allowlist on the real path so
    # neither ``..`` nor a symlink can escape a root.
    real = os.path.realpath(path)
    if _containing_root(real, roots) is None:
        raise HTTPException(422, "path is outside the allowed browse roots")
    if not os.path.isdir(real):
        raise HTTPException(404, "path is not an existing directory")

    dirs: list[dict[str, str]] = []
    try:
        with os.scandir(real) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                    child = os.path.join(real, entry.name)
                    # Re-resolve: a symlinked subdirectory is allowed ONLY if its
                    # target still lands within a root (never follow symlinks out).
                    if _containing_root(os.path.realpath(child), roots) is None:
                        continue
                    dirs.append({"name": entry.name, "path": child})
                except OSError:
                    # Unreadable / broken entry -> skip silently.
                    continue
    except OSError as exc:
        raise HTTPException(403, "directory is not readable") from exc

    dirs.sort(key=lambda d: d["name"])
    dirs = dirs[:_MAX_ENTRIES]

    # Parent: '' (back to the roots list) when at a root, else the containing dir
    # so long as it too stays within a root.
    if real in roots:
        parent: str | None = ""
    else:
        parent_path = os.path.dirname(real)
        parent = parent_path if _containing_root(parent_path, roots) else ""

    return {"path": real, "parent": parent, "roots": roots, "dirs": dirs}


@router.get("/browse", dependencies=[Depends(require_scope("admin"))])
async def browse(path: str = Query("", description="Absolute directory path to list; "
                                   "empty lists the configured roots.")) -> dict:
    return await run_in_threadpool(_browse, path)
