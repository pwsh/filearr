"""Content-addressed thumbnail keying + in-process generation (S12/P12 slice 1).

Pure, dependency-light core for the thumbnail cache. NO database, NO Procrastinate,
NO FastAPI here -- those live in ``filearr.tasks.thumbs`` (jobs/GC) and
``filearr.api.items`` (serve). This module owns two concerns only:

  1. **Keying / on-disk layout** -- ``cache_key`` + ``fanout_path``. The key is a
     hash digest (blake2b of ``hash:generator_version:tier``), so the on-disk path
     is derived ENTIRELY from a locally-computed digest, never from a filename or
     any request input. It is traversal-proof *by construction*: a blake2b hex
     digest is ``[0-9a-f]{32}`` -- no ``/`` and no ``..`` can appear, so no code
     path can escape the cache root (research §8 "cache-path traversal").

  2. **Generation** -- ``generate_image_thumb`` (Pillow decode -> downscale to the
     tier's longest edge -> WebP quality-ladder to fit a hard byte cap) and
     ``extract_audio_cover`` (mutagen embedded APIC/covr bytes). Both are used for
     untrusted, possibly hostile files, so decoding is bounded (pixel ceiling +
     Pillow's own decompression-bomb guard) and every failure is swallowed as a
     ``None`` return -- exactly the extractor discipline (never kill a batch for
     one hostile file).

Encoder ruling (S12 slice 1): **Pillow, not pyvips.** The research recommended
libvips for its streaming decode + throughput, and pyvips 3.1.1 is on PyPI -- but
pyvips is only a *binding*; it needs the libvips C library installed as a system
package (a new apt dependency + an independent CVE-tracking surface, the very
"second image-library security surface" concern FIX-7 raised about pillow-simd).
Pillow 12.3.0 is ALREADY a pinned, CVE-clean dependency with built-in WebP encode;
at slice-1 scope (images + audio covers, 320/800 px tiers) it reaches correctness
parity, and its MAX_IMAGE_PIXELS bomb guard + our pixel ceiling cover the OOM
threat model. The vips swap is recorded as a throughput optimization to benchmark
in a later slice (its 3-5x speed / 4x memory win pays off at 1M-item batch scale,
where it should be measured before adoption).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger("filearr.thumbs")

# Generator version -- baked into every cache key. Bump when the pipeline's OUTPUT
# would change (encoder swap, ladder retune) so old thumbnails become unaddressed
# and are lazily replaced, then GC-reclaimed. NOT bumped for pure code refactors.
GENERATOR_VERSION = 1

# Tier identifiers (persisted as ``thumbnail_manifest.tier`` smallint). ``grid`` is
# the search/browse square; ``preview`` is the larger detail/lightbox image.
TIER_GRID = 0
TIER_PREVIEW = 1

TIER_NAMES = {TIER_GRID: "grid", TIER_PREVIEW: "preview"}
TIER_BY_NAME = {v: k for k, v in TIER_NAMES.items()}


def tier_from_name(name: str) -> int | None:
    """Map a serve-path ``?tier=`` value to its smallint, or ``None`` if unknown.

    The ONLY place a request string touches the tier: a strict allowlist, never
    string-concatenated into a path (research §8)."""
    return TIER_BY_NAME.get(name)


@dataclass(frozen=True)
class TierSpec:
    """Resolved parameters for one tier (built from Settings so they stay
    operator-overridable)."""

    tier: int
    name: str
    max_edge: int
    quality: int
    max_bytes: int


def grid_spec(settings) -> TierSpec:
    return TierSpec(
        TIER_GRID,
        "grid",
        settings.thumbnail_grid_px,
        settings.thumbnail_grid_quality,
        settings.thumbnail_grid_max_bytes,
    )


def preview_spec(settings) -> TierSpec:
    return TierSpec(
        TIER_PREVIEW,
        "preview",
        settings.thumbnail_preview_px,
        settings.thumbnail_preview_quality,
        settings.thumbnail_preview_max_bytes,
    )


def spec_for(settings, tier: int) -> TierSpec:
    return grid_spec(settings) if tier == TIER_GRID else preview_spec(settings)


# --- keying / layout -------------------------------------------------------- #

def cache_key(hash_used: str, generator_version: int, tier: int) -> str:
    """Content-addressed cache key: ``blake2b(hash:gen_version:tier)`` hex (32 chars).

    ``hash_used`` is the item's ``content_hash`` (fallback ``quick_hash``) -- when
    the file changes its hash changes, so the old key is simply never looked up
    again (no invalidation bookkeeping; research §4). The digest is hex only, so
    the key is safe to place directly in a filesystem path.
    """
    basis = f"{hash_used}:{generator_version}:{tier}".encode()
    return hashlib.blake2b(basis, digest_size=16).hexdigest()


def fanout_path(key: str) -> str:
    """git-style 2-level fanout relative path for a cache key: ``ab/cd/<key>.webp``.

    Evenly distributes files (<=256 top dirs) so no single directory holds
    millions of entries. ``key`` is a trusted 32-hex digest from :func:`cache_key`;
    the length/charset assertion makes any accidental caller misuse fail loudly
    rather than produce a traversable path.
    """
    if len(key) < 4 or not all(c in "0123456789abcdef" for c in key):
        raise ValueError("cache key must be a lowercase hex digest")
    return f"{key[:2]}/{key[2:4]}/{key}.webp"


def thumbs_root(settings) -> str:
    """Absolute cache root: ``{config_dir}/thumbnails``."""
    return os.path.join(settings.config_dir, "thumbnails")


def abs_path(settings, key: str) -> str:
    """Absolute on-disk path for a cache key (root + fanout)."""
    return os.path.join(thumbs_root(settings), fanout_path(key))


# --- generation ------------------------------------------------------------- #

@dataclass(frozen=True)
class ThumbBytes:
    """A successfully encoded thumbnail: WebP bytes + final dimensions."""

    data: bytes
    width: int
    height: int


def _encode_webp_capped(img, spec: TierSpec, *, quality_floor: int, quality_step: int):
    """Downscale ``img`` (a Pillow image) to ``spec.max_edge`` longest edge, then
    step WebP quality down until the encoded size fits ``spec.max_bytes``.

    Returns ``ThumbBytes`` or ``None`` when even the quality floor overshoots the
    cap (store nothing rather than an oversized derivative). ``img`` is expected
    to already be loaded + within the pixel ceiling (checked by the caller).
    """
    from PIL import Image

    work = img
    if work.mode not in ("RGB", "RGBA"):
        # Flatten palette/CMYK/L etc. to a WebP-encodable mode. Preserve alpha
        # where present so transparent art (PNG posters) stays clean.
        work = work.convert("RGBA" if "A" in work.getbands() else "RGB")

    # Longest-edge downscale, never upscale (thumbnail() only shrinks).
    work = work.copy()
    work.thumbnail((spec.max_edge, spec.max_edge), Image.LANCZOS)

    quality = spec.quality
    while quality >= quality_floor:
        buf = io.BytesIO()
        work.save(buf, format="WEBP", quality=quality, method=4)
        data = buf.getvalue()
        if len(data) <= spec.max_bytes:
            return ThumbBytes(data=data, width=work.width, height=work.height)
        quality -= quality_step
    return None


def _open_guarded(source, settings):
    """Open an image from a path or raw bytes with the pixel-count guard armed.

    Returns a loaded Pillow image or ``None`` (undecodable / over the pixel
    ceiling / any error). Never raises -- hostile input must degrade to ``None``.
    """
    from PIL import Image

    try:
        if isinstance(source, (bytes, bytearray)):
            img = Image.open(io.BytesIO(bytes(source)))
        else:
            img = Image.open(source)
        # Header-declared size check BEFORE full decode (Pillow reads dims lazily),
        # so an oversized image is rejected without materialising its pixels.
        w, h = img.size
        if w * h > settings.thumbnail_max_pixels:
            img.close()
            return None
        img.load()  # force decode now so a truncated/corrupt file fails here
        return img
    except Exception:
        # Pillow's DecompressionBombError, UnidentifiedImageError, OSError on a
        # truncated file, etc. -- all collapse to "no thumbnail".
        return None


def generate_image_thumb(src_path: str, tier: int, settings) -> ThumbBytes | None:
    """Generate a WebP thumbnail for one tier from an image file at ``src_path``.

    Decode (guarded) -> downscale to the tier's longest edge -> WebP quality ladder
    to fit the tier byte cap. Returns ``None`` on any failure (undecodable,
    oversized source, over-cap even at the floor). Never raises.
    """
    img = _open_guarded(src_path, settings)
    if img is None:
        return None
    try:
        return _encode_webp_capped(
            img,
            spec_for(settings, tier),
            quality_floor=settings.thumbnail_quality_floor,
            quality_step=settings.thumbnail_quality_step,
        )
    except Exception:
        return None
    finally:
        img.close()


def generate_thumb_from_bytes(raw: bytes, tier: int, settings) -> ThumbBytes | None:
    """Same as :func:`generate_image_thumb` but from in-memory image bytes
    (embedded cover art, sidecar bytes)."""
    img = _open_guarded(raw, settings)
    if img is None:
        return None
    try:
        return _encode_webp_capped(
            img,
            spec_for(settings, tier),
            quality_floor=settings.thumbnail_quality_floor,
            quality_step=settings.thumbnail_quality_step,
        )
    except Exception:
        return None
    finally:
        img.close()


def extract_audio_cover(path: str) -> bytes | None:
    """Extract embedded cover-art bytes from an audio file via mutagen.

    Handles the common frame layouts: ID3 ``APIC`` (mp3), MP4/M4A/M4B ``covr``,
    FLAC/Ogg ``pictures``, and any tag exposing an ``APIC:``-prefixed key. Returns
    the FIRST cover's raw bytes or ``None``. mutagen is already a trusted, imported
    dependency (T6 runs it against these same files) -- no new trust boundary.
    Never raises.
    """
    try:
        import mutagen
        from mutagen.flac import FLAC, Picture
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4
    except Exception:  # pragma: no cover - mutagen is a hard dep
        return None

    # 1. MP4/M4A/M4B 'covr' atom.
    try:
        mp4 = MP4(path)
        covrs = mp4.tags.get("covr") if mp4.tags else None
        if covrs:
            return bytes(covrs[0])
    except Exception:
        pass

    # 2. FLAC embedded pictures.
    try:
        flac = FLAC(path)
        if flac.pictures:
            return bytes(flac.pictures[0].data)
    except Exception:
        pass

    # 3. ID3 APIC frames (mp3 and any ID3-tagged container).
    try:
        id3 = ID3(path)
        apics = id3.getall("APIC")
        if apics:
            return bytes(apics[0].data)
    except Exception:
        pass

    # 4. Generic fallback: mutagen.File may expose 'APIC:'/'metadata_block_picture'.
    try:
        f = mutagen.File(path)
        if f is not None and getattr(f, "tags", None):
            for key, val in f.tags.items():
                if str(key).startswith("APIC"):
                    return bytes(getattr(val, "data", val))
            # Ogg/Opus base64 FLAC Picture in 'metadata_block_picture'.
            mbp = f.tags.get("metadata_block_picture") if hasattr(f.tags, "get") else None
            if mbp:
                import base64

                pic = Picture(base64.b64decode(mbp[0]))
                return bytes(pic.data)
    except Exception:
        pass
    return None


# --- video poster-frame (P12 slice 2; OPS-T7 QSV-ready) --------------------- #

# Probe result for hardware acceleration, computed once per process. ``None`` =
# not yet probed; a bool thereafter. The probe is a cheap ``os.path.exists`` on
# the DRI render node -- no device is opened here, so it is safe to cache for the
# life of the worker (a /dev/dri that appears mid-run is picked up on restart).
_ACCEL_PROBED: bool | None = None
# One-time log guards so a fallback / probe result is logged ONCE per boot, never
# once per file (research: a hardware miss on a 115k-video library must not spam).
_ACCEL_LOGGED = False
_FALLBACK_LOGGED = False

_RENDER_NODE = "/dev/dri/renderD128"


def _accel_available() -> bool:
    """Whether an iGPU render node is present (cached once per process).

    A bare existence check on ``/dev/dri/renderD128`` -- the QSV/VAAPI device
    node the compose ``devices:`` mapping passes in (OPS-T7). Absent on a plain
    CPU host / this CI image, so 'auto' degrades to software with zero attempts.
    """
    global _ACCEL_PROBED, _ACCEL_LOGGED
    if _ACCEL_PROBED is None:
        _ACCEL_PROBED = os.path.exists(_RENDER_NODE)
        if not _ACCEL_LOGGED:
            log.info(
                "thumbnail hw-accel probe: %s %s",
                _RENDER_NODE,
                "present (QSV will be attempted)" if _ACCEL_PROBED else "absent (software only)",
            )
            _ACCEL_LOGGED = True
    return _ACCEL_PROBED


def _seek_seconds(duration_s: float | None, min_seek: float) -> float:
    """Seek target for the poster frame: ``max(min_seek, 10% of duration)``,
    clamped below ``duration-0.5s`` so a short clip never seeks past its end.

    With no known duration we fall back to ``min_seek`` (skip the black intro but
    make no percentage assumption) -- the research §generation ruling."""
    if duration_s and duration_s > 0:
        seek = max(min_seek, 0.10 * duration_s)
        # Never seek past (nearly) the end of a short clip -- clamp to a point
        # that still has a frame to decode.
        return min(seek, max(0.0, duration_s - 0.5))
    return min_seek


# The HDR->SDR tonemap chain (only injected for HDR sources). zscale + tonemap are
# both compiled into the image's ffmpeg (verified); the chain is fragile on
# mistagged inputs, so a failure is caught and retried WITHOUT it.
_TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)


def _scale_filter(max_edge: int) -> str:
    """ffmpeg scale filter: fit within ``max_edge`` x ``max_edge`` preserving AR,
    never upscaling (``min(edge,iw/ih)``)."""
    return (
        f"scale='min({max_edge},iw)':'min({max_edge},ih)':"
        "force_original_aspect_ratio=decrease"
    )


def _video_frame_argv(
    binary: str,
    src_path: str,
    seek: float,
    max_edge: int,
    *,
    accel: str | None,
    tonemap: bool,
) -> list[str]:
    """Build the argv for a single-frame grab. ``accel`` in {'qsv', None}.

    ``-ss`` is placed BEFORE ``-i`` (fast input seek), one frame is decoded, the
    frame is scaled to the tier edge INSIDE ffmpeg (so the PNG piped to stdout is
    already small). The untrusted path is isolated as the value of ``-i`` (a
    leading-dash filename can never be read as an option there); ``--`` then ``-``
    marks stdout as the output unambiguously. PNG (lossless) is the
    intermediate; the WebP quality-ladder runs afterwards in Pillow, reusing the
    exact image pipeline (one encoder, one byte-cap policy for every source)."""
    argv = [binary, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if accel == "qsv":
        # Decode-only acceleration; frames auto-download for the software scale
        # filter. Any init/decode failure exits nonzero -> caller falls back.
        argv += ["-hwaccel", "qsv"]
    vf = f"{_TONEMAP_CHAIN},{_scale_filter(max_edge)}" if tonemap else _scale_filter(max_edge)
    argv += [
        "-ss", f"{seek:.3f}",
        "-i", src_path,
        "-map", "0:v:0",           # first video stream only (skip attached art)
        "-frames:v", "1",
        "-vf", vf,
        "-f", "image2pipe",
        "-vcodec", "png",
        "--",
        "-",                        # write the PNG to stdout
    ]
    return argv


def _run_frame_grab(argv: list[str], timeout_s: float, max_bytes: int) -> bytes | None:
    """Run one frame-grab argv; return PNG bytes on success or ``None``.

    Mirrors ffprobe.py's posture: no shell, hard timeout (child killed), output
    capped. Any failure (missing binary, timeout, nonzero exit, empty/oversized
    output) collapses to ``None`` so the caller can try the next strategy or give
    up -- a bad video never raises."""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            shell=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    if len(proc.stdout) > max_bytes:
        return None
    return proc.stdout


def generate_video_thumb(
    src_path: str,
    tier: int,
    duration_s: float | None,
    settings,
    *,
    accel: str | None = None,
    hdr: bool = False,
) -> ThumbBytes | None:
    """Generate a WebP poster-frame thumbnail for a video at ``src_path``.

    Seeks to ``max(min_seek, 10% of duration)`` (fast pre-input ``-ss``), decodes
    ONE frame scaled to the tier edge, then runs it through the SAME Pillow WebP
    quality-ladder every other source uses. Returns ``None`` on any failure.

    Acceleration (OPS-T7) is a swappable policy behind one argv builder: ``accel``
    resolves from ``settings.thumb_accel`` ('auto'|'off'); 'auto' tries ``qsv``
    first ONLY when ``/dev/dri`` is present, then always falls back to software
    transparently (logged once per boot). HDR sources additionally attempt a
    tonemap chain, retried without it on failure."""
    global _FALLBACK_LOGGED

    binary = shutil.which(settings.ffmpeg_path)
    if binary is None:
        return None

    seek = _seek_seconds(duration_s, settings.thumbnail_video_min_seek_s)
    spec = spec_for(settings, tier)
    timeout_s = settings.thumb_ffmpeg_timeout_s
    max_bytes = settings.thumbnail_video_max_frame_bytes

    policy = (accel or settings.thumb_accel or "off").lower()
    # Ordered decode strategies; first success wins. 'auto' prepends qsv only when
    # a render node is present, and ALWAYS keeps software as the guaranteed tail.
    accels: list[str | None] = []
    if policy == "auto" and _accel_available():
        accels.append("qsv")
    accels.append(None)  # software -- always the final, guaranteed strategy

    want_tonemap = bool(hdr and settings.thumb_hdr_tonemap)

    for strat in accels:
        # For an HDR source, try WITH tonemap first, then without (fragile chain).
        for tm in ((True, False) if want_tonemap else (False,)):
            argv = _video_frame_argv(
                binary, src_path, seek, spec.max_edge, accel=strat, tonemap=tm
            )
            png = _run_frame_grab(argv, timeout_s, max_bytes)
            if png is not None:
                tb = generate_thumb_from_bytes(png, tier, settings)
                if tb is not None:
                    fell_back = (
                        strat is None
                        and policy == "auto"
                        and _accel_available()
                        and not _FALLBACK_LOGGED
                    )
                    if fell_back:
                        # qsv was attempted (device present) but software produced
                        # the frame -- log the fallback ONCE, never per file.
                        log.warning("thumbnail QSV path failed; using software decode")
                        _FALLBACK_LOGGED = True
                    return tb
    return None


# --- PDF first-page render (P12 slice 2 remainder; P12-T5) ------------------- #
#
# pypdfium2 == the Apache-2.0 / BSD-3-Clause Python binding over Google's PDFium
# (bundled libpdfium is BSD-style) -- both AGPL-compatible, no copyleft
# obligation, and the wheel BUNDLES the shared library so NO apt package is added
# to the image (unlike libvips/poppler). Chosen over pdftoppm (GPL poppler-utils
# CLI): in-process, no per-file subprocess spawn, cleaner license story
# (research §"PDF/documents"). It parses UNTRUSTED, possibly hostile PDFs, so the
# discipline mirrors the document extractor exactly:
#   * a source-BYTES gate BEFORE opening (reuse ``document_max_bytes``);
#   * a render pixel budget so an absurd page box can't balloon the bitmap;
#   * encrypted/password-protected, zero-page, and malformed PDFs all raise
#     ``PdfiumError`` on load/render -- every path collapses to ``None`` (never a
#     raise), exactly like the corrupt-video handling.
#
# Timeout note: pypdfium2 is IN-PROCESS (no subprocess to signal-kill). PDFium's
# page-1 decode is bounded work -- there is no user-controlled loop to run away.
# The sanity gate is therefore a SIZE + page-count check (source bytes <=
# ``document_max_bytes``; document must load with >=1 page) rather than a
# wall-clock signal, which would be unreliable against a C library holding the
# GIL anyway. This matches the research ruling for the in-process PDF path.


def generate_pdf_thumb(src_path: str, tier: int, settings) -> ThumbBytes | None:
    """Render page 1 of a PDF at ``src_path`` into a WebP thumbnail for ``tier``.

    Renders the first page at a scale targeting the tier's longest edge (so the
    intermediate bitmap is inherently small), converts RGB -> Pillow, then feeds
    the SAME WebP quality-ladder every other source uses (one encoder, one
    byte-cap policy). Returns ``None`` on ANY failure (oversized source, encrypted/
    password-protected, zero-page, malformed, render error). Never raises.
    """
    # Source-bytes gate BEFORE the parser opens the file (mirrors the document
    # extractor's ``document_max_bytes`` ceiling -- a huge/hostile PDF is rejected
    # without materialising it). A missing/unstat-able file also short-circuits.
    try:
        if os.path.getsize(src_path) > settings.document_max_bytes:
            return None
    except OSError:
        return None

    try:
        import pypdfium2 as pdfium
    except Exception:  # pragma: no cover - pypdfium2 is a hard dep in the image
        return None

    pdf = None
    try:
        # An encrypted/password PDF or a malformed file raises PdfiumError here.
        pdf = pdfium.PdfDocument(src_path)
        if len(pdf) < 1:  # defensive: pdfium already refuses a 0-page load
            return None
        page = pdf[0]
        w_pt, h_pt = page.get_size()
        longest = max(w_pt, h_pt)
        if longest <= 0:
            return None

        spec = spec_for(settings, tier)
        # Scale so the longest edge lands on the tier target (points -> pixels).
        scale = spec.max_edge / longest
        # Belt-and-braces render-buffer cap: never let the intermediate bitmap
        # exceed the pixel budget (bounds memory on an absurd page box).
        budget = settings.thumbnail_pdf_max_pixels
        if (w_pt * scale) * (h_pt * scale) > budget:
            scale = (budget / (w_pt * h_pt)) ** 0.5

        bitmap = page.render(scale=scale)
        pil = bitmap.to_pil()
        try:
            return _encode_webp_capped(
                pil,
                spec,
                quality_floor=settings.thumbnail_quality_floor,
                quality_step=settings.thumbnail_quality_step,
            )
        finally:
            try:
                pil.close()
            except Exception:
                pass
    except Exception:
        # PdfiumError (bad/encrypted/zero-page), decode error, anything -> no thumb.
        return None
    finally:
        if pdf is not None:
            try:
                pdf.close()
            except Exception:
                pass
