"""Sidecar file detection (T3).

A *sidecar* is a non-primary file that describes or decorates a real media item:
Kodi ``.nfo`` metadata, ``poster.jpg`` / ``folder.jpg`` artwork, ``*-thumb.jpg``
thumbnails, ``*_JRSidecar.xml`` JRiver metadata, subtitle-adjacent art, etc. They
pollute default search results as bogus top-level ``other``/``image`` hits, so we
detect them during scan, link them to their parent media item via ``sidecar_of``,
and exclude them from default search (still filterable).

This module is pure/deterministic: it classifies by *path shape only* and never
touches the filesystem or the DB. The scan task owns association + persistence.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Directory-level artwork: names that decorate the *containing folder* (the
# folder's primary media item), not a specific stem. Kodi/Plex/Emby conventions.
# Compared case-insensitively against the full filename.
_DIR_ARTWORK_NAMES: frozenset[str] = frozenset(
    {
        "poster.jpg", "poster.png", "poster.jpeg",
        "folder.jpg", "folder.png", "folder.jpeg",
        "cover.jpg", "cover.png", "cover.jpeg",
        "fanart.jpg", "fanart.png", "fanart.jpeg",
        "banner.jpg", "banner.png", "banner.jpeg",
        "clearart.jpg", "clearart.png",
        "clearlogo.jpg", "clearlogo.png",
        "landscape.jpg", "landscape.png",
        "thumb.jpg", "thumb.png",
        "logo.jpg", "logo.png",
        "disc.jpg", "disc.png",
        "season-all-poster.jpg", "season-all-banner.jpg",
    }
)

# Extensions that, as a *whole file*, are always sidecar metadata (never primary).
_ALWAYS_SIDECAR_EXTS: frozenset[str] = frozenset({".nfo"})

# Extensions that are ALWAYS a per-item sidecar keyed on the SAME STEM as the
# media file they describe (never directory-level, unlike a bare movie.nfo):
#   * ``.xmp`` — Adobe/XMP metadata sidecar ("IMG_1234.xmp" -> "IMG_1234.<raw>")
#   * ``.thm`` — camera thumbnail ("MVI_5678.thm" -> "MVI_5678.<video>")
# Maps ext -> emitted ``kind``. A file whose stem is empty (a bare dotfile like
# ".xmp") never reaches here: ``os.path.splitext(".xmp")`` yields ext="" so it
# falls through as a non-sidecar (no parent stem -> not a sidecar).
_STEM_SIDECAR_EXTS: dict[str, str] = {".xmp": "xmp", ".thm": "artwork"}

# Stem suffixes that mark a per-item sidecar; e.g. "Movie (2020)-thumb.jpg" or
# "Movie (2020)-poster.jpg" belong to "Movie (2020).<video-ext>". Lowercase.
_STEM_SUFFIXES: tuple[str, ...] = (
    "-thumb", "-poster", "-fanart", "-banner", "-landscape",
    "-clearart", "-clearlogo", "-disc", "-logo",
)

# Image extensions eligible to be a per-stem / directory artwork sidecar.
_ART_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".webp", ".tbn"})


@dataclass(frozen=True)
class SidecarInfo:
    """Result of classifying a path as a sidecar.

    kind        — 'nfo' | 'jriver' | 'artwork' | 'xmp'
    parent_stem — stem of the sibling media file this sidecar belongs to, or
                  ``None`` for directory-level artwork (→ the folder's primary item)
    directory   — the sidecar's containing directory (posix rel form as given)
    """

    kind: str
    parent_stem: str | None
    directory: str


def _split(rel_path: str) -> tuple[str, str, str, str]:
    """Return (directory, filename, stem, ext_lower) for a relative path."""
    directory = os.path.dirname(rel_path)
    filename = os.path.basename(rel_path)
    stem, ext = os.path.splitext(filename)
    return directory, filename, stem, ext.lower()


def classify(rel_path: str) -> SidecarInfo | None:
    """Classify a relative path. Returns ``None`` if it is NOT a sidecar.

    Detection is purely lexical so it is cheap and re-scan idempotent.
    """
    directory, filename, stem, ext = _split(rel_path)
    fname_lower = filename.lower()
    stem_lower = stem.lower()

    # 1. JRiver sidecar: "<anything>_JRSidecar.xml" (detect + link only; parsing
    #    is a future metadata source — JRiver has no ecosystem API).
    if fname_lower.endswith("_jrsidecar.xml"):
        base_stem = stem[: -len("_JRSidecar")] if stem_lower.endswith("_jrsidecar") else None
        return SidecarInfo(kind="jriver", parent_stem=base_stem or None, directory=directory)

    # 2. .nfo — Kodi/Emby metadata. "<stem>.nfo" → parent stem; but a bare
    #    "movie.nfo"/"tvshow.nfo" is directory-level (whole-folder metadata).
    if ext in _ALWAYS_SIDECAR_EXTS:
        if stem_lower in ("movie", "tvshow", "season", "album", "artist"):
            return SidecarInfo(kind="nfo", parent_stem=None, directory=directory)
        return SidecarInfo(kind="nfo", parent_stem=stem, directory=directory)

    # 2b. Same-stem-only metadata/thumbnail sidecars (.xmp / .thm). Always a
    #     per-item sidecar of the sibling sharing the stem; never directory-
    #     level. A non-empty stem is required (a bare '.xmp' dotfile has ext=''
    #     and never lands here), so 'no parent stem' => not a sidecar.
    kind = _STEM_SIDECAR_EXTS.get(ext)
    if kind is not None and stem:
        return SidecarInfo(kind=kind, parent_stem=stem, directory=directory)

    # 3. Artwork images.
    if ext in _ART_EXTS:
        # 3a. Directory-level artwork by conventional filename.
        if fname_lower in _DIR_ARTWORK_NAMES:
            return SidecarInfo(kind="artwork", parent_stem=None, directory=directory)
        # 3b. Per-stem artwork: "<parent-stem><suffix>.<ext>".
        for suffix in _STEM_SUFFIXES:
            if stem_lower.endswith(suffix):
                base = stem[: -len(suffix)]
                if base:
                    return SidecarInfo(kind="artwork", parent_stem=base, directory=directory)
                # e.g. "-poster.jpg" with empty base → directory artwork.
                return SidecarInfo(kind="artwork", parent_stem=None, directory=directory)
        # 3c. "SxxEyy-thumb.tbn" style already covered; season posters:
        if stem_lower.startswith("season") and ("poster" in stem_lower or "banner" in stem_lower):
            return SidecarInfo(kind="artwork", parent_stem=None, directory=directory)

    return None
