"""EXIF/GPS extraction stub + the default-hidden GPS gate (Phase 3 — P3-T11).

**Inert scaffolding.** Only tests import this module. It ships the implemented,
pure ``strip_gps`` gate (the security-tier default that keeps location metadata
out of the Meili projection and public API unless a library explicitly opts in)
plus the ``extract_exif`` stub that P3-T11 implements over an exiftool
``-stay_open`` subprocess pool.

Security rationale (brief §5 / §10): exposing GPS by default is CWE-1230
("Exposure of Sensitive Information Through Metadata"), a real vulnerability class
(CVE-2023-1974, CVE-2026-27892) with concrete real-world harm (McAfee's 2012
location leak; the "Cybercasing the Joint" study). No mainstream self-hosted photo
tool ships a safe default here — Filearr does. GPS fields still land in
``metadata_`` (extracted, invariant 2) as normal, but ``strip_gps`` removes them
from anything externally exposed unless the per-library ``expose_gps`` toggle
(default false, R5) is on. P3-T11 wires the extractor and this gate in the **same
commit** — never GPS extraction first and the gate as a follow-up.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

# Canonical GPS / location tag names emitted by exiftool across still-image EXIF,
# XMP, and the QuickTime/MP4 timed-location streams the brief calls out (dashcam /
# drone / smartphone tracks that ffprobe + mutagen cannot see). Matching in
# ``strip_gps`` is case-insensitive AND also fires on any key whose name begins
# with ``gps`` (exiftool namespaces dozens of ``GPS*`` sub-tags), so this frozenset
# is the explicit, documented core plus the non-``GPS``-prefixed location keys.
GPS_KEYS: frozenset[str] = frozenset(
    {
        # EXIF GPS IFD
        "GPSLatitude",
        "GPSLatitudeRef",
        "GPSLongitude",
        "GPSLongitudeRef",
        "GPSAltitude",
        "GPSAltitudeRef",
        "GPSPosition",
        "GPSCoordinates",
        "GPSTimeStamp",
        "GPSDateStamp",
        "GPSDateTime",
        "GPSMapDatum",
        "GPSProcessingMethod",
        "GPSImgDirection",
        "GPSImgDirectionRef",
        "GPSDestBearing",
        "GPSSpeed",
        "GPSSpeedRef",
        "GPSHPositioningError",
        # Non-"GPS"-prefixed location keys (QuickTime / Apple / composite / geo)
        "_geo",
        "geo",
        "location",
        "ISO6709",
        "com.apple.quicktime.location.ISO6709",
        "com.apple.quicktime.location.accuracy.horizontal",
    }
)

# Lower-cased lookup set for case-insensitive membership.
_GPS_KEYS_LOWER: frozenset[str] = frozenset(k.lower() for k in GPS_KEYS)


def _is_gps_key(key: str) -> bool:
    """A key is a GPS/location key if it is (case-insensitively) a known member,
    begins with the ``gps`` tag prefix exiftool uses for its GPS sub-tags, OR its
    final dotted segment is GPS-shaped.

    The last-segment rule covers the P4 R5 namespaced convention (``exif.gps_*``):
    ``exif.gps_latitude`` is a GPS key (segment ``gps_latitude`` starts with
    ``gps``) while ``exif.camera_make`` is not. Flat legacy keys (``GPSLatitude``)
    still match via the prefix/membership checks, so the existing gate behaviour is
    unchanged."""
    low = key.lower()
    if low in _GPS_KEYS_LOWER or low.startswith("gps"):
        return True
    last = low.rpartition(".")[2]
    return last.startswith("gps") or last in _GPS_KEYS_LOWER


def strip_gps(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``metadata`` with every GPS/location key removed,
    recursing into nested dicts and into dicts inside lists.

    The default-hidden gate (brief §5): applied to what is projected into Meili
    and returned by the public API when a library's ``expose_gps`` is false. Pure
    and non-mutating — the caller's original ``metadata_`` (source of truth,
    invariant 2) is untouched; only the externally-exposed copy is filtered.
    """
    return _strip(metadata)


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if not _is_gps_key(k)}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


class ExifError(RuntimeError):
    """exiftool could not analyse the file (missing binary, timeout, nonzero exit,
    oversized/unparseable output). Message is safe to store under ``_exif_error``."""


# Curated exiftool tag -> Filearr ``exif.``-namespaced key (P4 R5). GPS keys are
# included: they are extracted and stored RAW in metadata_; the default-hidden gate
# (``strip_gps`` + per-library ``expose_gps``) decides external exposure, NOT the
# extractor. Run with ``-n`` so GPS coordinates come back as signed decimals and
# numeric tags as numbers (not exiftool's human-formatted strings).
_EXIF_FIELD_MAP: tuple[tuple[str, str, str], ...] = (
    # (source exiftool tag, target exif.* key, kind: 'str'|'num')
    ("Make", "exif.camera_make", "str"),
    ("Model", "exif.camera_model", "str"),
    ("LensModel", "exif.lens_model", "str"),
    ("LensID", "exif.lens_id", "str"),
    ("ISO", "exif.iso", "num"),
    ("ExposureTime", "exif.exposure_time", "num"),
    ("FNumber", "exif.f_number", "num"),
    ("FocalLength", "exif.focal_length", "num"),
    ("ImageWidth", "exif.width", "num"),
    ("ImageHeight", "exif.height", "num"),
    ("DateTimeOriginal", "exif.taken_at", "str"),
    ("CreateDate", "exif.taken_at", "str"),  # fallback when DateTimeOriginal absent
    ("GPSLatitude", "exif.gps_latitude", "num"),
    ("GPSLongitude", "exif.gps_longitude", "num"),
    ("GPSAltitude", "exif.gps_altitude", "num"),
)

_STR_CAP = 500


def _resolve(binary: str) -> str:
    resolved = shutil.which(binary)
    if resolved is None:
        raise ExifError(f"exiftool not found: {binary!r}")
    return resolved


def run_exiftool(
    path: str,
    *,
    exiftool_path: str = "exiftool",
    timeout_s: float = 30.0,
    max_output_bytes: int = 8_388_608,
) -> dict[str, Any]:
    """Run ``exiftool -json -n <file>`` and return the parsed tag dict.

    One subprocess per file (``-stay_open`` pooling is a scan-wide optimisation the
    worker layer can add later; a per-item extract job does not need it — brief §5).
    argv list (never a shell string), hard timeout (child killed), bounded stdout.
    Raises :class:`ExifError` on any failure.
    """
    binary = _resolve(exiftool_path)
    argv = [binary, "-json", "-n", "-charset", "filename=utf8", path]
    try:
        proc = subprocess.run(
            argv, capture_output=True, timeout=timeout_s, check=False, shell=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ExifError(f"exiftool timed out after {timeout_s:g}s") from exc
    except OSError as exc:
        raise ExifError(f"exiftool could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = detail[-1] if detail else f"exit {proc.returncode}"
        raise ExifError(f"exiftool failed: {msg}")
    if len(proc.stdout) > max_output_bytes:
        raise ExifError(
            f"exiftool output too large ({len(proc.stdout)} > {max_output_bytes} bytes)"
        )
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExifError(f"exiftool output not valid JSON: {exc}") from exc
    # exiftool -json emits a list with one object per input file.
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ExifError("exiftool output was not a JSON object")
    return data


def _num(v: Any) -> float | int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def _str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s[:_STR_CAP] if s else None


def map_exif_tags(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a raw exiftool tag dict onto the curated ``exif.*`` namespace.

    Pure + defensively typed (untrusted parser output): unknown/odd values are
    dropped rather than raised, numeric tags coerced, strings length-capped. A
    tag mapped to an already-populated target (``CreateDate`` after
    ``DateTimeOriginal``) does NOT overwrite it (first non-empty wins). GPS keys
    are emitted here RAW; the exposure gate lives entirely in ``strip_gps`` +
    ``expose_gps``.
    """
    out: dict[str, Any] = {}
    for src, target, kind in _EXIF_FIELD_MAP:
        if src not in raw:
            continue
        val = _num(raw[src]) if kind == "num" else _str(raw[src])
        if val is None:
            continue
        out.setdefault(target, val)
    return out


def extract_exif(
    path: str,
    *,
    exiftool_path: str = "exiftool",
    timeout_s: float = 30.0,
    max_output_bytes: int = 8_388_608,
) -> dict[str, Any]:
    """Extract curated ``exif.*`` metadata for ``path`` via an exiftool subprocess.

    Camera/lens/exposure/dimension/timestamp + GPS fields land in ``metadata_``
    (invariant 2). GPS is stored RAW; ``strip_gps`` + the per-library ``expose_gps``
    toggle gate external exposure (never exiv2 in-process bindings — GPL linking
    ambiguity). Raises :class:`ExifError` on failure so the caller records an
    ``_exif_error`` sentinel without failing the whole extract.
    """
    raw = run_exiftool(
        path,
        exiftool_path=exiftool_path,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    return map_exif_tags(raw)
