"""P11-T7 — low-quality-video scoring heuristic (pure function).

A single, dependency-free, deterministic scorer over an item's
``effective_metadata`` (the user overlay merged onto extracted ffprobe fields —
:pyattr:`filearr.models.Item.effective_metadata`). It reads ONLY keys that
:mod:`filearr.tasks.ffprobe` already normalises (``height``/``width``/
``video_codec``/``bitrate``/``hdr``/``color_transfer``/``audio_tracks`` …) — it
never triggers new extraction and never persists a value (a stored "quality
score" would be neither extracted nor user-edited data, violating invariant 2).

The additive component model, the resolution/codec floors, and the banding are
taken from `docs/research/phase-11-reporting-exports.md` §3, whose bitrate
floors are DERIVED from the TRaSH-Guides Radarr quality-size table:
https://trash-guides.info/Radarr/Radarr-Quality-Settings-File-Size/

Design mirrors ``querydsl.py``'s philosophy: pure, no I/O, returns *why* it
fired (component reasons) so a report column can explain a number, not just show
it. All thresholds live as module constants below so a future Phase-4
profile override (research Open Question 5) can supply its own table without
touching the algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Thresholds (research §3 / §2). Bits-per-pixel-per-second ("BPP") floors are   #
# derived from TRaSH's published MB/min minimums (§2 table, bits/px/s column),  #
# taking the *WEB-DL* row per resolution — the lowest tier — so only files      #
# below even efficient-streaming density are flagged (conservative: fewest      #
# false positives, matching the standing priority order's reliability lean).    #
# --------------------------------------------------------------------------- #

#: A file this many pixels tall or shorter is categorically sub-HD (TRaSH FAQ:
#: sub-720p is undesirable on modern displays). Drives the +40 resolution signal.
SUB_HD_HEIGHT = 720

#: video_codec values treated as legacy/obsolete. Deliberately excludes bare
#: ``mpeg4`` (ffprobe's ``video_codec`` cannot distinguish DivX/Xvid from generic
#: MPEG-4 ASP without ``codec_tag_string``, which is not captured — research §3).
LEGACY_CODECS = frozenset(
    {"mpeg1video", "mpeg2video", "msmpeg4v2", "msmpeg4v3", "h263", "wmv1", "wmv2"}
)

#: Efficient modern codecs (~2x density of h.264/mpeg2): their BPP floor halves.
EFFICIENT_CODECS = frozenset({"hevc", "av1", "vp9"})

#: Audio codecs considered lossless (their presence rules out the 4K-downmix
#: signal — a lossless track means the source was not aggressively re-encoded).
LOSSLESS_AUDIO = frozenset({"truehd", "dts", "dts-hd", "flac", "mlp", "pcm"})

#: SDR-typical color-transfer values: an ``hdr=true`` item carrying one of these
#: (or no transfer at all) is a likely mislabeled/mis-remuxed HDR flag.
SDR_TRANSFERS = frozenset({"", "bt709", "bt601", "smpte170m", "bt470bg"})

#: HDR-typical color-transfer values (PQ / HLG): their presence on a NOT-hdr 4K
#: item is the reverse mislabel case.
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})

#: bits/px/s floor by resolution tier (WEB-DL rows, research §2). Keys are the
#: minimum height for the tier; the largest key <= height applies. NOTE the
#: published table's "bits/px/s" column is derived with bitrate in **kilobits**
#: (TRaSH MB/min -> kbps / pixels), so :func:`score_item` compares against a BPP
#: computed the same way (``(bitrate_bps / 1000) / (width*height)``).
BPP_FLOORS: dict[int, float] = {
    720: 0.00181,
    1080: 0.00080,
    2160: 0.00055,
}

# Component point weights (research §3).
POINTS_SUB_HD = 40
POINTS_LEGACY_CODEC = 25
POINTS_BITRATE_MAX = 25
POINTS_HDR_MISMATCH = 10
POINTS_AUDIO_DOWNMIX = 10

# Banding (research §3; config-tunable there, constants here for v1).
REVIEW_BAND = 25  # score >= this: at least "review"
REACQUIRE_BAND = 50  # score >= this: re-acquisition candidate

BAND_OK = "ok"
BAND_REVIEW = "review"
BAND_REACQUIRE = "reacquire"


@dataclass(frozen=True)
class ScoreResult:
    """Outcome of :func:`score_item`: a total, its band, and the reasons that
    fired (human-readable, order-stable — safe to join into a report cell)."""

    score: int
    band: str
    reasons: list[str] = field(default_factory=list)


def _num(value: object) -> float | None:
    """Coerce a metadata value to a positive float, or None (missing/bad type).

    Metadata is JSONB — numbers may arrive as int/float/str; anything non-finite,
    non-positive, or unparseable is treated as absent (skip, never raise)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")) or f <= 0:
        return None
    return f


def _bpp_floor(height: int, codec: str) -> float | None:
    """Resolution-tier BPP floor for ``height`` (largest tier key <= height),
    halved when ``codec`` is efficient. None below the smallest tier (SD — the
    resolution signal already covers sub-HD, so no bitrate floor is applied)."""
    tier = None
    for key in sorted(BPP_FLOORS):
        if height >= key:
            tier = key
    if tier is None:
        return None
    floor = BPP_FLOORS[tier]
    if codec in EFFICIENT_CODECS:
        floor /= 2
    return floor


def score_item(effective_metadata: dict) -> ScoreResult:
    """Score one item's effective metadata for low-video-quality signals.

    Pure and deterministic. Unknown/missing/oddly-typed fields are skipped, never
    raised (metadata is best-effort and may be partial). Returns the additive
    total, its band, and one reason string per component that fired."""
    md = effective_metadata or {}
    score = 0
    reasons: list[str] = []

    codec = str(md.get("video_codec") or "").lower()
    height = _num(md.get("height"))
    width = _num(md.get("width"))

    # 1. Sub-HD resolution.
    if height is not None and height < SUB_HD_HEIGHT:
        score += POINTS_SUB_HD
        reasons.append(f"sub-HD resolution ({int(height)}p < {SUB_HD_HEIGHT}p)")

    # 2. Legacy/obsolete codec.
    if codec in LEGACY_CODECS:
        score += POINTS_LEGACY_CODEC
        reasons.append(f"legacy codec ({codec})")

    # 3. Bitrate-per-pixel below the resolution/codec floor (0..25, linear;
    #    >=50% below floor = full 25).
    bitrate = _num(md.get("bitrate"))
    if bitrate is not None and width is not None and height is not None and height >= SUB_HD_HEIGHT:
        floor = _bpp_floor(int(height), codec)
        if floor is not None:
            # kilobits/px/s to match the TRaSH-derived floor units (see BPP_FLOORS).
            bpp = (bitrate / 1000.0) / (width * height)
            if bpp < floor:
                deficit = (floor - bpp) / (floor * 0.5)  # 1.0 at 50%-below-floor
                deficit = min(max(deficit, 0.0), 1.0)
                pts = round(POINTS_BITRATE_MAX * deficit)
                if pts > 0:
                    score += pts
                    reasons.append(
                        f"low bitrate ({bpp:.5f} kbpp < {floor:.5f} floor, +{pts})"
                    )

    # 4. HDR/color-metadata mismatch.
    transfer = str(md.get("color_transfer") or "").lower()
    is_hdr = bool(md.get("hdr"))
    hdr_mismatch = False
    if is_hdr and transfer in SDR_TRANSFERS:
        hdr_mismatch = True
        reasons.append("HDR flagged but SDR/absent color transfer")
    elif (
        not is_hdr
        and height is not None
        and height >= 2160
        and transfer in HDR_TRANSFERS
    ):
        hdr_mismatch = True
        reasons.append("PQ/HLG transfer on a non-HDR-flagged 4K item")
    if hdr_mismatch:
        score += POINTS_HDR_MISMATCH

    # 5. 4K stereo-only / lossy audio downmix oddity.
    tracks = md.get("audio_tracks")
    if height is not None and height >= 2160 and isinstance(tracks, list) and tracks:
        def _stereo_lossy(t: object) -> bool:
            if not isinstance(t, dict):
                return False
            ch = _num(t.get("channels"))
            codec_a = str(t.get("codec") or "").lower()
            return (ch is not None and ch <= 2) and codec_a not in LOSSLESS_AUDIO

        if all(_stereo_lossy(t) for t in tracks):
            score += POINTS_AUDIO_DOWNMIX
            reasons.append("4K source with stereo-only lossy audio")

    if score >= REACQUIRE_BAND:
        band = BAND_REACQUIRE
    elif score >= REVIEW_BAND:
        band = BAND_REVIEW
    else:
        band = BAND_OK
    return ScoreResult(score=score, band=band, reasons=reasons)
