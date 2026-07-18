"""ffprobe-based technical metadata extraction for video items.

Invokes the system ffprobe (path from ``FILEARR_FFPROBE_PATH``) with a JSON
output format, a hard runtime timeout, and a bounded read of stdout. Filenames
are passed as an argv list (never a shell string), so no argument the file's
name might contain can inject a command. Parsing is defensive: ffprobe's JSON is
untrusted input and any missing/oddly-typed field is skipped rather than raised.

Public surface:
    probe(path)          -> raw parsed ffprobe dict (raises on failure)
    extract_video_tech(path) -> normalised metadata dict for Item.metadata_

The normalised schema (all keys optional; absent when unknown):
    container       str    format short name(s), e.g. "matroska,webm"
    duration        float  seconds
    bitrate         int    bits/sec (container-level)
    video_codec     str    e.g. "h264", "hevc", "av1"
    width           int
    height          int
    resolution      str    "WxH", e.g. "1920x1080"
    frame_rate      float  avg fps
    hdr             bool    true when HDR signalling detected
    hdr_format      str     "HDR10"/"HDR10+"/"Dolby Vision"/"HLG" when identifiable
    color_primaries str
    color_transfer  str
    audio_codec     str    codec of the first/default audio track (convenience)
    audio_tracks    list[dict]  {codec, channels, channel_layout, language, title, default}
    subtitle_tracks list[dict]  {codec, language, title, forced, default}
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


class FfprobeError(RuntimeError):
    """ffprobe could not analyse the file (missing binary, timeout, nonzero
    exit, unparseable/oversized output). Message is safe to store in metadata."""


def _resolve_binary(ffprobe_path: str) -> str:
    """Resolve the configured ffprobe to an executable path, or raise."""
    resolved = shutil.which(ffprobe_path)
    if resolved is None:
        raise FfprobeError(f"ffprobe not found: {ffprobe_path!r}")
    return resolved


def probe(
    path: str,
    *,
    ffprobe_path: str = "ffprobe",
    timeout_s: float = 30.0,
    max_output_bytes: int = 8_388_608,
) -> dict[str, Any]:
    """Run ffprobe on ``path`` and return its parsed JSON.

    Raises FfprobeError on any failure (missing binary, timeout, nonzero exit,
    oversized or unparseable output). The child process is killed on timeout.
    """
    binary = _resolve_binary(ffprobe_path)
    argv = [
        binary,
        "-v", "error",
        "-hide_banner",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "--",
        path,  # untrusted; safe as a list arg (no shell interpretation)
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:  # child already killed by subprocess
        raise FfprobeError(f"ffprobe timed out after {timeout_s:g}s") from exc
    except OSError as exc:
        raise FfprobeError(f"ffprobe could not run: {exc}") from exc

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = detail[-1] if detail else f"exit {proc.returncode}"
        raise FfprobeError(f"ffprobe failed: {msg}")

    if len(proc.stdout) > max_output_bytes:
        raise FfprobeError(
            f"ffprobe output too large ({len(proc.stdout)} > {max_output_bytes} bytes)"
        )

    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise FfprobeError(f"ffprobe output not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FfprobeError("ffprobe output was not a JSON object")
    return data


# --- normalisation helpers (all tolerant of missing/odd values) --------------

def _as_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fps(rate: Any) -> float | None:
    """Parse ffprobe's 'num/den' frame-rate string into fps."""
    if not isinstance(rate, str) or "/" not in rate:
        return _as_float(rate)
    num, _, den = rate.partition("/")
    n, d = _as_float(num), _as_float(den)
    if n is None or not d:
        return None
    return round(n / d, 3)


def _lang(tags: dict[str, Any]) -> str | None:
    lang = tags.get("language") or tags.get("LANGUAGE")
    if isinstance(lang, str) and lang and lang.lower() != "und":
        return lang
    return None


def _title(tags: dict[str, Any]) -> str | None:
    t = tags.get("title") or tags.get("TITLE")
    return t if isinstance(t, str) and t else None


def _detect_hdr(stream: dict[str, Any]) -> tuple[bool, str | None]:
    """Best-effort HDR detection from a video stream's colour signalling."""
    transfer = (stream.get("color_transfer") or "").lower()
    primaries = (stream.get("color_primaries") or "").lower()
    side = stream.get("side_data_list") or []
    side_types = {
        (d.get("side_data_type") or "").lower()
        for d in side
        if isinstance(d, dict)
    }

    if "dovi configuration record" in side_types or "dolby vision" in side_types:
        return True, "Dolby Vision"
    if transfer == "arib-std-b67" or primaries == "bt2020":
        # HLG uses the arib-std-b67 transfer; treat bt2020 primaries as HDR wide-gamut.
        if transfer == "arib-std-b67":
            return True, "HLG"
    if transfer in ("smpte2084", "smptest2084"):
        has_plus = any("dynamic hdr" in t or "hdr10+" in t for t in side_types)
        return True, "HDR10+" if has_plus else "HDR10"
    if primaries == "bt2020":
        return True, None
    return False, None


def extract_video_tech(
    path: str,
    *,
    ffprobe_path: str = "ffprobe",
    timeout_s: float = 30.0,
    max_output_bytes: int = 8_388_608,
) -> dict[str, Any]:
    """Return normalised technical metadata for a video file.

    Raises FfprobeError on probe failure so the caller can record ``_extract_error``.
    """
    data = probe(
        path,
        ffprobe_path=ffprobe_path,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []

    meta: dict[str, Any] = {}

    container = fmt.get("format_name")
    if isinstance(container, str) and container:
        meta["container"] = container
    if (dur := _as_float(fmt.get("duration"))) is not None:
        meta["duration"] = round(dur, 3)
    if (br := _as_int(fmt.get("bit_rate"))) is not None:
        meta["bitrate"] = br

    audio_tracks: list[dict[str, Any]] = []
    subtitle_tracks: list[dict[str, Any]] = []
    video_seen = False

    for s in streams:
        if not isinstance(s, dict):
            continue
        kind = s.get("codec_type")
        tags = s.get("tags") if isinstance(s.get("tags"), dict) else {}
        disp = s.get("disposition") if isinstance(s.get("disposition"), dict) else {}

        if kind == "video" and not video_seen:
            # attached cover art / thumbnails are single frames, not the main track
            if disp.get("attached_pic"):
                continue
            video_seen = True
            if isinstance(s.get("codec_name"), str):
                meta["video_codec"] = s["codec_name"]
            w, h = _as_int(s.get("width")), _as_int(s.get("height"))
            if w is not None and h is not None:
                meta["width"] = w
                meta["height"] = h
                meta["resolution"] = f"{w}x{h}"
            if (fr := _fps(s.get("avg_frame_rate"))) is not None and fr > 0:
                meta["frame_rate"] = fr
            hdr, hdr_fmt = _detect_hdr(s)
            if hdr:
                meta["hdr"] = True
                if hdr_fmt:
                    meta["hdr_format"] = hdr_fmt
            for key in ("color_primaries", "color_transfer"):
                if isinstance(s.get(key), str) and s[key]:
                    meta[key] = s[key]
        elif kind == "audio":
            track = {
                k: v
                for k, v in {
                    "codec": s.get("codec_name"),
                    "channels": _as_int(s.get("channels")),
                    "channel_layout": s.get("channel_layout"),
                    "language": _lang(tags),
                    "title": _title(tags),
                    "default": bool(disp.get("default")),
                }.items()
                if v is not None
            }
            audio_tracks.append(track)
        elif kind == "subtitle":
            track = {
                k: v
                for k, v in {
                    "codec": s.get("codec_name"),
                    "language": _lang(tags),
                    "title": _title(tags),
                    "forced": bool(disp.get("forced")),
                    "default": bool(disp.get("default")),
                }.items()
                if v is not None
            }
            subtitle_tracks.append(track)

    if audio_tracks:
        meta["audio_tracks"] = audio_tracks
        first_codec = audio_tracks[0].get("codec")
        if isinstance(first_codec, str):
            meta["audio_codec"] = first_codec
    if subtitle_tracks:
        meta["subtitle_tracks"] = subtitle_tracks

    return meta
