"""trimesh-based geometry metadata for 3D model items.

Loads a mesh with trimesh and reports lightweight geometry facts — triangle and
vertex counts, bounding-box dimensions, and the watertight flag — without
retaining the loaded mesh. Multi-mesh scenes (a GLTF/GLB/3MF holding several
meshes) are aggregated: counts summed, bounds taken over the whole scene.

Security / reliability discipline mirrors the ffprobe extractor:
    * A hard file-size ceiling is enforced *before* handing the path to trimesh
      (trimesh reads the entire mesh into RAM, so an unbounded file is an OOM
      vector). The caller passes ``max_bytes`` from FILEARR_MODEL3D_MAX_BYTES.
    * trimesh is invoked with ``process=False`` so it does no expensive mesh
      repair/merging on untrusted geometry, and network fetches are impossible
      (we never enable trimesh's remote-resolver; a local path only).
    * Any parse failure raises Model3DError with a message safe to store; the
      caller records it under ``_extract_error`` and the job stays green.

Only formats trimesh can load as geometry are parsed: STL, OBJ, PLY, GLTF, GLB,
3MF (and OFF). STEP/STP, FBX, and BLEND have no safe pure-Python loader in
trimesh's default stack, so they are reported as ``unsupported`` rather than
parsed (still no error — just no geometry facts).

Emitted metadata schema (all keys optional; absent when unknown):
    triangles      int      total face count across all meshes
    vertices       int      total vertex count across all meshes
    mesh_count     int      number of meshes (1 for a single mesh; >1 for scenes)
    bbox           list[3]  bounding-box extents [dx, dy, dz] (source units)
    bbox_volume    float    dx*dy*dz
    watertight     bool     true only when every mesh is watertight
    file_format    str      trimesh-detected loader key, e.g. "stl", "glb"
    unsupported    bool     true when the extension has no geometry loader here
"""

from __future__ import annotations

import os
from pathlib import PurePath
from typing import Any

# Extensions trimesh can load as geometry with its default, dependency-free
# stack. Deliberately excludes step/stp/fbx/blend (no safe pure loader).
_GEOMETRY_EXTS = {"stl", "obj", "ply", "off", "gltf", "glb", "3mf"}


class Model3DError(RuntimeError):
    """A 3D model could not be parsed (too large, unreadable, unloadable).
    Message is safe to store in metadata."""


def _round(v: Any, ndigits: int = 4) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, ndigits) if f == f else None  # drop NaN


def _iter_meshes(loaded: Any):
    """Yield every Trimesh in a loaded object (a bare mesh or a Scene)."""
    import trimesh

    if isinstance(loaded, trimesh.Trimesh):
        yield loaded
        return
    geometry = getattr(loaded, "geometry", None)
    if isinstance(geometry, dict):
        for g in geometry.values():
            if isinstance(g, trimesh.Trimesh):
                yield g


def extract_model3d(path: str, *, max_bytes: int) -> dict[str, Any]:
    """Return geometry metadata for a 3D model at ``path``.

    Raises Model3DError on any failure (oversized, unreadable, unloadable) so the
    caller can record ``_extract_error``. Files whose extension has no geometry
    loader return ``{"unsupported": True}`` (not an error).
    """
    ext = PurePath(path).suffix.lstrip(".").lower()
    if ext not in _GEOMETRY_EXTS:
        return {"unsupported": True}

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise Model3DError(f"cannot stat model: {exc}") from exc
    if size > max_bytes:
        raise Model3DError(f"model too large ({size} > {max_bytes} bytes)")

    import trimesh

    try:
        # force="mesh" would merge scenes; keep the natural type so scene bounds
        # are exact. process=False: no repair/merge work on untrusted geometry.
        loaded = trimesh.load(path, process=False)
    except Exception as exc:  # trimesh raises a zoo of exception types
        raise Model3DError(f"trimesh could not load model: {exc}") from exc

    meshes = list(_iter_meshes(loaded))
    if not meshes:
        raise Model3DError("no mesh geometry found in file")

    triangles = 0
    vertices = 0
    watertight = True
    for m in meshes:
        faces = getattr(m, "faces", None)
        verts = getattr(m, "vertices", None)
        if faces is not None:
            triangles += len(faces)
        if verts is not None:
            vertices += len(verts)
        # .is_watertight can itself raise on degenerate meshes — stay defensive.
        try:
            if not bool(m.is_watertight):
                watertight = False
        except Exception:
            watertight = False

    meta: dict[str, Any] = {
        "triangles": triangles,
        "vertices": vertices,
        "mesh_count": len(meshes),
        "watertight": watertight,
    }
    if isinstance(ext, str) and ext:
        meta["file_format"] = ext

    # Scene/mesh bounds → extents. `bounds` is a (2,3) array; extents = max-min.
    bounds = getattr(loaded, "bounds", None)
    if bounds is not None:
        try:
            dims = [_round(bounds[1][i] - bounds[0][i]) for i in range(3)]
            if all(d is not None for d in dims):
                meta["bbox"] = dims
                vol = dims[0] * dims[1] * dims[2]
                meta["bbox_volume"] = round(vol, 6)
        except (IndexError, TypeError):
            pass

    return meta
