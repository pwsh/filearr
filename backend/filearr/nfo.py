"""Kodi NFO parsing (T3).

NFO files are *untrusted input* from the scanned filesystem, so XML is parsed
defensively with ``defusedxml`` (entity expansion, external entities and DTDs are
refused). Some ".nfo" files are not XML at all — a plain URL or free text (an old
Kodi convention). Those are handled gracefully: we return ``{}`` rather than raise.

Only a conservative, well-understood subset of Kodi fields is mapped into the
parent item's *extracted* ``metadata`` (never ``user_metadata``). Unknown roots
are ignored.
"""

from __future__ import annotations

from typing import Any
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _defused_fromstring

# Recognised Kodi NFO root tags → logical media kind (for provenance only).
_KNOWN_ROOTS = {"movie", "episodedetails", "tvshow", "musicvideo"}

# Max NFO size we will parse — a metadata sidecar is tiny; anything larger is
# almost certainly not a real NFO (defends against accidental huge input).
MAX_NFO_BYTES = 512 * 1024


def _text(elem: Any, tag: str) -> str | None:
    child = elem.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _int(elem: Any, tag: str) -> int | None:
    raw = _text(elem, tag)
    if raw is None:
        return None
    # Kodi sometimes stores "2020-01-01" in <year>/<premiered>; take leading digits.
    digits = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        else:
            break
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def parse_nfo_bytes(data: bytes) -> dict[str, Any]:
    """Parse NFO bytes into a metadata dict. Never raises on malformed input.

    Returns ``{}`` for non-XML / plain-text / malicious / empty NFO files.
    """
    if not data or len(data) > MAX_NFO_BYTES:
        return {}
    try:
        # defusedxml refuses DTDs, external + parameter entities and entity bombs.
        root = _defused_fromstring(data)
    except (DefusedXmlException, ParseError, ValueError):
        # Malicious (blocked), malformed, or plain-text NFO — treat as no metadata.
        return {}
    except Exception:
        return {}

    tag = (root.tag or "").lower()
    if tag not in _KNOWN_ROOTS:
        return {}

    out: dict[str, Any] = {"nfo_kind": tag}

    def put(key: str, value: Any) -> None:
        if value is not None and value != "":
            out[key] = value

    put("title", _text(root, "title"))
    put("original_title", _text(root, "originaltitle"))
    put("plot", _text(root, "plot") or _text(root, "outline"))
    put("year", _int(root, "year"))
    put("tagline", _text(root, "tagline"))
    put("studio", _text(root, "studio"))
    put("mpaa", _text(root, "mpaa"))
    put("runtime", _int(root, "runtime"))

    if tag == "episodedetails":
        put("season", _int(root, "season"))
        put("episode", _int(root, "episode"))
        put("aired", _text(root, "aired"))

    # <genre> may repeat.
    genres = [g.text.strip() for g in root.findall("genre") if g.text and g.text.strip()]
    if genres:
        out["genre"] = genres if len(genres) > 1 else genres[0]

    # rating: prefer <rating value="..."> then bare <rating>text.
    rating_elem = root.find("rating")
    if rating_elem is not None:
        rv = rating_elem.get("value") or (rating_elem.text or "").strip()
        try:
            if rv:
                out["rating"] = float(rv)
        except ValueError:
            pass

    # External IDs Kodi commonly embeds (<uniqueid type="imdb">tt...</uniqueid>).
    ext_ids: dict[str, str] = {}
    for uid in root.findall("uniqueid"):
        kind = (uid.get("type") or "").strip().lower()
        val = (uid.text or "").strip()
        if kind and val:
            ext_ids[kind] = val
    imdb = _text(root, "imdbid") or _text(root, "id")
    if imdb and imdb.startswith("tt"):
        ext_ids.setdefault("imdb", imdb)
    if ext_ids:
        out["external_ids"] = ext_ids

    return out
