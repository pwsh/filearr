"""P3-T11 — EXIF deep extraction pass (images v1) + the GPS default-hidden gate.

A thin worker wrapper over ``filearr.exif.extract_exif`` (one exiftool subprocess
per file). Curated camera/lens/exposure/dimension/timestamp + GPS fields are
projected under the P4 R5 ``exif.*`` namespace and merged into ``metadata_``
(invariant 2). GPS fields are stored RAW here; the default-hidden gate
(``exif.strip_gps`` + the per-library ``expose_gps`` toggle) removes them from the
Meili projection (``search.build_doc``) and the public API responses unless the
library opted in — the extractor NEVER makes the exposure decision (R5, CWE-1230).

v1 runs for the ``image`` media type only. exiftool ALSO reads timed GPS-track
streams from MOV/MP4 containers (dashcam/drone/phone) that ffprobe cannot see — a
sourced gap in the T1 video pipeline (brief §5). That supplemental video-GPS pass
is a roadmap follow-up (it needs the same ``expose_gps`` gate, already in place),
not part of this v1 change.
"""

from __future__ import annotations

from typing import Any

from filearr.config import Settings
from filearr.errors import sanitize_error
from filearr.exif import ExifError, extract_exif


def exif_metadata(path: str, *, settings: Settings) -> dict[str, Any]:
    """Return the ``exif.*`` metadata delta for ``path`` (or an error sentinel).

    Never raises: an exiftool failure degrades to ``{"_exif_error": ...}`` so the
    supplementary EXIF pass can never fail the whole extract. GPS keys, when
    present, are returned RAW — the exposure gate lives in ``strip_gps`` +
    ``expose_gps``, not here.
    """
    try:
        return extract_exif(
            path,
            exiftool_path=settings.exiftool_path,
            timeout_s=settings.exif_timeout_s,
            max_output_bytes=settings.exif_max_output_bytes,
        )
    except ExifError as exc:
        return {"_exif_error": sanitize_error(exc)}
    except Exception as exc:  # defence in depth
        return {"_exif_error": sanitize_error(exc)}
