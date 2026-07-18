"""Audiobook (m4b/mp4) chapter extraction via mutagen.

Audiobooks already flow through the tinytag audio extractor for standard tags
(title/artist/album/duration). This module adds the one thing tinytag does not
surface: the embedded chapter list (QuickTime/Nero ``chpl`` chapters), read with
mutagen's MP4 chapter support.

Discipline matches the other T6 extractors: mutagen does no network I/O and only
reads the file's atom tree (not the audio payload), so it is bounded by nature;
any failure raises AudiobookError with a message safe to store under
``_extract_error``. Files without chapters simply return ``{}`` — not an error.

Emitted schema (only added when chapters are present):
    chapters        list[dict]  each {title:str?, start:float(seconds)}
    chapter_count   int
"""

from __future__ import annotations

from typing import Any


class AudiobookError(RuntimeError):
    """Chapter data could not be read. Message is safe to store in metadata."""


def _as_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 3) if f == f else None  # drop NaN


def extract_chapters(path: str) -> dict[str, Any]:
    """Return chapter metadata for an m4b/mp4 audiobook.

    Raises AudiobookError if the container cannot be parsed. Returns ``{}`` when
    the file parses but carries no chapters.
    """
    from mutagen import MutagenError
    from mutagen.mp4 import MP4

    try:
        mp4 = MP4(path)
    except MutagenError as exc:
        raise AudiobookError(f"mutagen could not read audiobook: {exc}") from exc
    except Exception as exc:
        raise AudiobookError(f"mutagen failed: {exc}") from exc

    chapters_obj = getattr(mp4, "chapters", None)
    if not chapters_obj:
        return {}

    chapters: list[dict[str, Any]] = []
    try:
        for ch in chapters_obj:
            entry: dict[str, Any] = {}
            title = getattr(ch, "title", None)
            if isinstance(title, str) and title.strip():
                entry["title"] = title.strip()
            start = _as_float(getattr(ch, "start", None))
            if start is not None:
                entry["start"] = start
            if entry:
                chapters.append(entry)
    except Exception as exc:
        raise AudiobookError(f"could not iterate chapters: {exc}") from exc

    if not chapters:
        return {}
    return {"chapters": chapters, "chapter_count": len(chapters)}
