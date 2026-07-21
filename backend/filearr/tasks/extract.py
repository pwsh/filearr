"""Per-type metadata extraction. Populates Item.metadata_ (extracted facts only —
user_metadata is never touched here). Hashing: xxh3 quick-hash ALWAYS; the full
content_hash only when the library's resolved hash policy (T7) permits it and the
file is at/below the (per-library or global) size ceiling."""

import logging
import re
from typing import Any

import xxhash
from procrastinate.jobs import Job
from procrastinate.retry import BaseRetryStrategy, RetryDecision
from sqlalchemy import select, text

from filearr import taxonomy
from filearr.config import get_settings
from filearr.db import SessionLocal
from filearr.errors import sanitize_error
from filearr.hashpolicy import resolve_hash_policy
from filearr.models import Item, ItemVersion, Library, ScanRun
from filearr.profiles import validate_metadata
from filearr.provenance import policy_version
from filearr.tasks.archives import ArchiveError, is_archive, list_archive_members
from filearr.worker import proc_app

log = logging.getLogger("filearr.extract")

QUICK_CHUNK = 65536  # 64 KiB head + tail

# Cap applied to any string coerced from parsed data before it reaches a typed
# column. Matches errors.MAX_ERROR_CHARS so a single pathological tag can never
# bloat a row; ``title`` (Text) has no DB-level length limit, so we enforce one.
STR_CAP = 500


def coerce_year(raw: Any) -> int | None:
    """Coerce an arbitrary tag/parse value to a 4-digit year int, or ``None``.

    Live incident (class A/B): tinytag/guessit hand back a full date string
    (``"2007-10-09"``) or a multi-value list (``[2007, 2008]`` from a
    multi-year filename / multi-value tag). A bare ``int(raw)`` on the first
    dies with ``ValueError``; the list flows straight into ``item.year`` and
    kills the *commit* with a psycopg CannotCoerce (``smallint[]`` -> integer).

    This helper takes the first element of a list/tuple, scans the string form
    for the first ``19xx``/``20xx`` run, and returns ``None`` when nothing
    year-like is present. It NEVER raises — bad file data must not kill an
    extract. Unparseable values are dropped silently (the raw string is still
    preserved in ``metadata`` by the extractor that emitted it).
    """
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return None
    m = re.search(r"(19|20)\d{2}", str(raw))
    return int(m.group(0)) if m else None


def coerce_str(raw: Any, *, cap: int = STR_CAP) -> str | None:
    """Coerce an arbitrary tag/parse value to a trimmed, length-capped ``str``.

    Lists/tuples (multi-value tags) collapse to their first element; the result
    is stringified, stripped and truncated to ``cap`` characters. An empty
    result becomes ``None``. Never raises.
    """
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    return s[:cap]


def quick_hash(path: str, size: int) -> str:
    """xxh3-64 head/tail sampling probe (the fast move-detection tier).

    QH-T1 boundary edge (pinned identically in the Go mirror
    ``agent/internal/scan/hash.go``): a file whose ``size <= 2*QUICK_CHUNK``
    (<=131072 bytes — INCLUSIVE of the 128 KiB point) is hashed IN FULL; only a
    file ``size > 2*QUICK_CHUNK`` (strictly greater) is sampled as head+tail.

    The old code read a fixed ``QUICK_CHUNK`` head unconditionally and only added
    the tail above 131072, so a 64-128 KiB file had its middle+tail silently
    UNhashed — a genuine false-duplicate defect. Reading the whole small file is
    not a speed tradeoff (a size-appropriate single read is as cheap as the old
    partial read; see brief §5a)."""
    h = xxhash.xxh3_64()
    with open(path, "rb") as f:
        if size > QUICK_CHUNK * 2:
            # >128 KiB: sampled head + tail (unchanged, by design).
            h.update(f.read(QUICK_CHUNK))
            f.seek(-QUICK_CHUNK, 2)
            h.update(f.read(QUICK_CHUNK))
        else:
            # <=128 KiB: hash the WHOLE file. ``read()`` with no argument sizes the
            # buffer to the actual data — no fixed head cap, no 1 MiB over-alloc.
            h.update(f.read())
    return h.hexdigest()


def full_hash(path: str, size: int | None = None) -> str:
    """xxh3-128 hex digest (32 chars) over the WHOLE file (QH-T3).

    QH-T2/§5a buffer sizing: a small file (``size <= 2*QUICK_CHUNK``, or an
    unknown size) is read in a single ``read()`` sized to the data, avoiding the
    fixed 1 MiB buffer allocation the brief measured as ~85% of the small-file
    cost. Larger files stream in 1 MiB chunks (bounded memory)."""
    h = xxhash.xxh3_128()
    with open(path, "rb") as f:
        if size is not None and size <= QUICK_CHUNK * 2:
            h.update(f.read())
        else:
            while chunk := f.read(1 << 20):
                h.update(chunk)
    return h.hexdigest()


def full_hashes_migration(path: str, size: int | None = None) -> tuple[str, str]:
    """``(xxh3_128_hex32, xxh3_64_hex16)`` whole-file digests in ONE streaming
    pass — the QH-T3 migration-window helper.

    Catalog rows hashed before the xxh3-128 switch hold 16-hex xxh3-64
    ``content_hash`` values, and they persist until the cfg2 re-hash sweep (or,
    for agent-owned items the sweep excludes, an agent-side mtime change)
    replaces them. A byte-comparison consumer (the P10-T5 staging verify path)
    must therefore dispatch on the STORED digest's length and compare against the
    matching algorithm; this helper hands it both digests for the cost of one
    read. Delete once no 16-hex content_hash rows remain."""
    h128 = xxhash.xxh3_128()
    h64 = xxhash.xxh3_64()
    with open(path, "rb") as f:
        if size is not None and size <= QUICK_CHUNK * 2:
            data = f.read()
            h128.update(data)
            h64.update(data)
        else:
            while chunk := f.read(1 << 20):
                h128.update(chunk)
                h64.update(chunk)
    return h128.hexdigest(), h64.hexdigest()


def _mutagen_get(tags: Any, *names: str) -> Any:
    """Case-insensitive lookup across a mutagen tag mapping.

    APEv2 (Monkey's Audio / WavPack / Musepack) stores keys like ``Title`` /
    ``Year``; the easy interface uses lower-case (``title`` / ``date``). Search
    every candidate name case-insensitively and return the first hit (value may
    be a list, an APETextValue, etc. — the coercers tame it).
    """
    if not tags:
        return None
    try:
        lowmap = {str(k).lower(): k for k in tags.keys()}
    except Exception:
        return None
    for name in names:
        key = lowmap.get(name.lower())
        if key is not None:
            return tags.get(key)
    return None


def _extract_audio_mutagen(path: str) -> dict[str, Any]:
    """Fallback tag reader for formats tinytag rejects (APE/Monkey's Audio,
    WavPack, Musepack, ...).

    tinytag raises ``UnsupportedFormatError`` ("No tag reader found to support
    file type") for these; mutagen reads their APEv2/native tags. Text tags come
    from the easy-key interface (title/artist/album/genre/date); technical
    fields come from the stream ``.info``. Every value bound for a typed column
    is coerced. ``mutagen.File`` returns ``None`` when it also cannot identify
    the container — we return ``{}`` in that case and let the caller record a
    plain "no tags" result (never a crash).
    """
    import mutagen

    audio = mutagen.File(path, easy=True)
    if audio is None:
        return {}

    tags = getattr(audio, "tags", None) or {}
    meta: dict[str, Any] = {
        "title": coerce_str(_mutagen_get(tags, "title")),
        "artist": coerce_str(_mutagen_get(tags, "artist")),
        "album": coerce_str(_mutagen_get(tags, "album")),
        "genre": coerce_str(_mutagen_get(tags, "genre")),
        "year": coerce_year(_mutagen_get(tags, "date", "year", "originaldate")),
    }
    info = getattr(audio, "info", None)
    if info is not None:
        meta["duration"] = getattr(info, "length", None)
        meta["bitrate"] = getattr(info, "bitrate", None)
        meta["channels"] = getattr(info, "channels", None)
        meta["samplerate"] = getattr(info, "sample_rate", None)
    return {k: v for k, v in meta.items() if v is not None}


def extract_audio(path: str) -> dict[str, Any]:
    """Standard audio tags via tinytag, with a mutagen fallback for the formats
    tinytag has no reader for (APE/Monkey's Audio, WavPack, Musepack, ...).

    Every value that later feeds a TYPED column (``year``, ``title``) is run
    through the defensive coercers here so a full-date tag string
    (``"2007-10-09"``) or a multi-value list can never propagate as a non-int /
    array. Formats tinytag can't parse raise ``UnsupportedFormatError``, which we
    catch and retry via :func:`_extract_audio_mutagen`; a genuine parse failure
    (``ParseError``/other) still propagates so the caller records
    ``_extract_error``.
    """
    from tinytag import TinyTag, UnsupportedFormatError

    try:
        tag = TinyTag.get(path)
    except UnsupportedFormatError:
        return _extract_audio_mutagen(path)

    return {
        k: v
        for k, v in {
            "title": coerce_str(tag.title),
            "artist": coerce_str(tag.artist),
            "album": coerce_str(tag.album),
            "genre": coerce_str(tag.genre),
            "year": coerce_year(tag.year),
            "duration": tag.duration,
            "bitrate": tag.bitrate,
            "samplerate": tag.samplerate,
            "channels": tag.channels,
        }.items()
        if v is not None
    }


def extract_image(path: str) -> dict[str, Any]:
    from PIL import Image

    with Image.open(path) as img:
        meta: dict[str, Any] = {
            "width": img.width,
            "height": img.height,
            "format": img.format,
            "mode": img.mode,
        }
        exif = img.getexif()
        if exif:
            # 271/272 make/model, 306 datetime
            if exif.get(271) or exif.get(272):
                meta["camera"] = f"{exif.get(271, '')} {exif.get(272, '')}".strip()
            if exif.get(306):
                meta["taken_at"] = str(exif.get(306))
    return meta


def extract_video(path: str) -> dict[str, Any]:
    """Merge guessit's filename parse (title/year/episode) with ffprobe's
    technical metadata (codec/resolution/duration/tracks/HDR).

    ffprobe failures (corrupt/truncated file, timeout, missing binary) do NOT
    raise here: the guessit parse is still useful, so the probe error is recorded
    under ``_extract_error`` and returned alongside whatever was parsed. This
    keeps the extract job green while surfacing the failure (see T11).

    ``title``/``year`` are coerced (guessit can return a multi-year *list* for a
    filename like ``Movie.2007.2008.mkv`` — the root cause of the class-B commit
    failure) so they are safe to hand to the typed columns.
    """
    from guessit import guessit

    from filearr.config import get_settings
    from filearr.tasks.ffprobe import FfprobeError, extract_video_tech

    guessed = guessit(path)
    meta: dict[str, Any] = {
        k: v
        for k, v in {
            "title": coerce_str(guessed.get("title")),
            "year": coerce_year(guessed.get("year")),
            "season": guessed.get("season"),
            "episode": guessed.get("episode"),
        }.items()
        if v is not None
    }

    settings = get_settings()
    try:
        meta.update(
            extract_video_tech(
                path,
                ffprobe_path=settings.ffprobe_path,
                timeout_s=settings.ffprobe_timeout_s,
                max_output_bytes=settings.ffprobe_max_output_bytes,
            )
        )
    except FfprobeError as exc:
        meta["_extract_error"] = str(exc)

    return meta


def extract_audiobook(path: str) -> dict[str, Any]:
    """Standard audio tags (via the shared tinytag path) plus embedded chapters.

    The tinytag tag read and the mutagen chapter read fail independently: a
    chapter-parse failure records ``_extract_error`` but never discards the tags
    that were already parsed (mirrors the video extractor's ffprobe handling).
    """
    from filearr.tasks.audiobook import AudiobookError, extract_chapters

    meta = extract_audio(path)
    try:
        meta.update(extract_chapters(path))
    except AudiobookError as exc:
        meta["_extract_error"] = str(exc)
    return meta


def extract_model3d(path: str) -> dict[str, Any]:
    """trimesh geometry facts, bounded by FILEARR_MODEL3D_MAX_BYTES."""
    from filearr.config import get_settings
    from filearr.tasks.model3d import Model3DError
    from filearr.tasks.model3d import extract_model3d as _extract

    try:
        return _extract(path, max_bytes=get_settings().model3d_max_bytes)
    except Model3DError as exc:
        return {"_extract_error": str(exc)}


def extract_document(path: str) -> dict[str, Any]:
    """PDF/DOCX/XLSX properties PLUS a separately-bounded body-text pass (P3-T5).

    Bounded by FILEARR_DOCUMENT_MAX_BYTES (compressed ceiling) and, for the body
    pass, FILEARR_BODY_TEXT_MAX_CHARS (char ceiling). Handles both the ``document``
    and ``spreadsheet`` media types (dispatch is by extension inside). The body
    pass is INDEPENDENT: a body-parse failure records ``_extract_error`` but never
    discards the already-parsed properties; a decompression-bomb docx/xlsx is
    rejected by the same guard BEFORE either parser opens the archive.
    """
    from filearr.config import get_settings
    from filearr.tasks.documents import DocumentError, extract_body
    from filearr.tasks.documents import extract_document as _extract

    settings = get_settings()
    guard = {
        "decompressed_max": settings.doc_decompressed_max,
        "ratio_limit": settings.doc_decompression_ratio,
        "ratio_min_bytes": settings.doc_decompression_ratio_min_bytes,
    }
    try:
        meta = _extract(path, max_bytes=settings.document_max_bytes, **guard)
    except DocumentError as exc:
        # Property parse failed (or the bomb guard rejected the file) — record the
        # error and stop before the body pass (which would re-hit the same guard).
        return {"_extract_error": str(exc)}

    try:
        body = extract_body(
            path,
            max_chars=settings.body_text_max_chars,
            max_bytes=settings.document_max_bytes,
            **guard,
        )
    except DocumentError as exc:
        meta["_extract_error"] = str(exc)
        body = {}
    if body:
        # A txt/md file has no property parser (``unsupported`` marker); once body
        # text is extracted it IS supported, so drop the marker before merging.
        meta.pop("unsupported", None)
        meta.update(body)
    return meta


# W8-B extraction routing off the File Extension Similarity Taxonomy.
#
# The taxonomy CATEGORY carries an ``extractor`` KIND string (image/audio/video/
# document/model3d or None) — resolved per item via ``taxonomy.category_extractor``
# — which maps to the extractor fn here. This folds the old sample->audio and
# spreadsheet->document routings in automatically (``.wav`` is now file_category
# ``audio``; a spreadsheet is file_category ``document``).
EXTRACTOR_BY_KIND = {
    "image": extract_image,
    "audio": extract_audio,
    "video": extract_video,
    "model3d": extract_model3d,
    "document": extract_document,
}

# GROUP-level overrides: where a specific file_group needs a DIFFERENT extractor
# than its category's default. ``audiobook`` lives under the ``audio`` category
# (extractor kind ``audio`` -> extract_audio, tags only) but must run
# ``extract_audiobook`` to also pull embedded chapters — the one place the old
# per-MediaType routing made a distinction the category layer alone loses. Keyed
# on ``item.file_group`` and consulted BEFORE the category extractor.
EXTRACTOR_BY_GROUP = {
    "audiobook": extract_audiobook,
}


async def _resolve_extractor(session, item):
    """The extractor fn for ``item`` (W8-B): a ``file_group`` override wins, else
    the item's ``file_category`` extractor kind from the live taxonomy. ``None``
    when the category has no extractor (development/archive/system/other) or the
    row is unclassified."""
    override = EXTRACTOR_BY_GROUP.get(item.file_group)
    if override is not None:
        return override
    if not item.file_category:
        return None
    kind = await taxonomy.category_extractor(session, item.file_category)
    return EXTRACTOR_BY_KIND.get(kind) if kind else None


EXTRACT_MAX_ATTEMPTS = 2  # genuine-failure retry budget (was retry=2)


class RescheduleExtract(Exception):
    """Sentinel raised by :func:`extract_item` to ask the queue to run it again
    LATER because the staged pipeline gate is closed (a scan is walking the same
    library). It is NOT a failure -- :class:`StagedExtractRetry` turns it into an
    attempt-agnostic reschedule, never a ``failed`` job."""


class StagedExtractRetry(BaseRetryStrategy):
    """Retry strategy for ``extract_item`` (UI-T14).

    Two disjoint behaviours by exception kind:

    * :class:`RescheduleExtract` (the staged gate) -> ALWAYS reschedule at
      ``now + extract_reschedule_seconds``, regardless of ``job.attempts``. The
      gate is a wait, not a failure, so it must never give up and never mark the
      job ``failed`` no matter how long the scan runs.
    * any other exception (a genuine infra fault -- extract_item catches all
      parse/data errors internally and records ``_extract_error`` rather than
      raising, so a raised exception here is DB-down-class) -> mirror the old
      ``retry=2``: retry immediately up to ``EXTRACT_MAX_ATTEMPTS`` then give up.

    KNOWN, DOCUMENTED TRADEOFF: procrastinate's ``retry_job`` increments the
    single ``attempts`` counter on EVERY reschedule too (there is no separate
    counter in procrastinate 3.9), so many gate reschedules inflate ``attempts``;
    a genuine fault that happens AFTER them may thus find the budget already spent
    and fail without its 2 real retries. This is acceptable here: gate reschedules
    only occur while a scan walks the library (bounded), and genuine extract raises
    are rare infra faults the next scan's self-heal re-queues anyway."""

    def get_retry_decision(
        self, *, exception: BaseException, job: Job
    ) -> RetryDecision | None:
        if isinstance(exception, RescheduleExtract):
            return RetryDecision(
                retry_in={"seconds": get_settings().extract_reschedule_seconds}
            )
        if job.attempts >= EXTRACT_MAX_ATTEMPTS:
            return None
        return RetryDecision(retry_in={"seconds": 0})


@proc_app.task(
    queue="extract",
    name="filearr.tasks.extract.extract_item",
    retry=StagedExtractRetry(),
    priority=get_settings().extract_priority,
)
async def extract_item(item_id: str, scan_run_id: str | None = None) -> None:
    settings = get_settings()
    async with SessionLocal() as session:
        item = (
            await session.execute(select(Item).where(Item.id == item_id))
        ).scalar_one_or_none()
        if item is None:
            return  # row from an aborted batch — nothing to extract

        # Captured before the post-commit attribute expiry so the thumbnail
        # ride-along defer (after this session block) can gate on the taxonomy
        # classification without re-loading a detached row. file_category is
        # extension-derived and stable per item.
        item_file_category = item.file_category
        # Also captured pre-expiry: the thumbnail ride-along gates PDFs on the
        # extension (only ``.pdf`` renders among ``document`` items -- P12-T5).
        item_rel_path = item.rel_path

        # UI-T14 staged-pipeline runtime gate (FIRST real step, before any hash or
        # extract work, so a reschedule has ZERO side effects): if staged mode is on
        # and a scan is currently WALKING this item's library (a running/stopping
        # ScanRun exists), an old-queue extract must not fight the fresh scan's disk
        # /network I/O -- reschedule it (attempt-agnostic; see StagedExtractRetry)
        # and let the scan's own end-of-walk defer drive extraction once it lands.
        if settings.staged_pipeline:
            walking = (
                await session.execute(
                    select(ScanRun.id)
                    .where(
                        ScanRun.library_id == item.library_id,
                        ScanRun.status.in_(("running", "stopping")),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if walking is not None:
                raise RescheduleExtract(
                    "scan walking library; deferring extract"
                )

        # FIX-11 PG-volume pause: extraction writes DB rows, so when the Postgres
        # data volume is at the critical low-space floor we must not keep piling
        # work onto a database that can no longer grow — PAUSE by rescheduling
        # (reusing the staged gate's attempt-agnostic backoff, never a failure).
        # Opt-in: only engages when FILEARR_DISK_PG_PATH points at a path THIS
        # process can statvfs (the PG volume is invisible in the split-container
        # compose deploy; in the single-volume LXC it is set to /config). Inert
        # (default None) so a standard deploy is unaffected.
        if settings.disk_pg_path:
            from filearr import diskguard

            if diskguard.is_critical(settings.disk_pg_path, settings):
                raise RescheduleExtract(
                    "postgres volume critically low; pausing extract"
                )

        # Resolve the library's T7 hash policy for this file. The extract worker
        # runs in a separate process from the scan, so it re-resolves from the
        # library row (single source of truth in filearr.hashpolicy) rather than
        # trusting a value passed across the queue.
        library = (
            await session.execute(select(Library).where(Library.id == item.library_id))
        ).scalar_one_or_none()
        resolved = resolve_hash_policy(
            declared=library.hash_policy if library else "auto",
            root_path=library.root_path if library else "",
            hash_full_max_bytes=library.hash_full_max_bytes if library else None,
            global_max_bytes=settings.scan_hash_full_max_bytes,
        )

        # P4-T7: stamp the owning library's scan-relevant config fingerprint. NULL
        # only when the library row is somehow gone (defensive); otherwise every
        # extract records which config version produced this item's metadata_.
        if library is not None:
            item.policy_version = policy_version(library, settings)
        try:
            item.quick_hash = quick_hash(item.path, item.size)
            # QH-T2: a file <= 2*QUICK_CHUNK (128 KiB) ALWAYS gets a real
            # content_hash, independent of hash_policy (even quick_only) and of the
            # T7 ceiling — it is cheap enough to hash exactly (§5a) and a sampled
            # quick_hash is never trustworthy identity for it. A larger file keeps
            # the T7 policy gate: content_hash only when policy allows AND it fits
            # the (per-library or global) byte ceiling.
            if item.size <= QUICK_CHUNK * 2 or (
                resolved.compute_content and item.size <= resolved.full_max_bytes
            ):
                item.content_hash = full_hash(item.path, item.size)
        except OSError:
            pass

        extractor = await _resolve_extractor(session, item)
        extract_failed = False
        meta: dict[str, Any] = {}
        if extractor is not None:
            try:
                meta = extractor(item.path)
                # Extractors that swallow their own failure (video/audiobook/
                # model3d/document) report it via a ``_extract_error`` key in the
                # returned dict rather than raising -- treat that as a failure too.
                if "_extract_error" in meta:
                    extract_failed = True
            except Exception as exc:  # extraction must never kill the scan
                # T11: error strings are untrusted (filenames/parser output) --
                # sanitize + length-cap before persisting into JSONB.
                meta = {"_extract_error": sanitize_error(exc)}
                extract_failed = True

        # P3-T11 EXIF deep extraction (images v1). Curated exif.* keys — camera/
        # lens/exposure/dimension/timestamp + GPS — merge into metadata_. GPS is
        # stored RAW; the exposure decision lives entirely in the strip_gps gate +
        # library.expose_gps (R5, CWE-1230), never in the extractor. Supplementary
        # and independent: an exiftool failure records _exif_error but does NOT
        # fail the extract.
        if item.file_category == "image":
            from filearr.tasks.exif_run import exif_metadata

            meta.update(exif_metadata(item.path, settings=settings))

        # P3-T6 OCR pass (per-library opt-in, R4). Runs ONLY when the owning
        # library opted in, so the default (global OFF) pays zero OCR cost. The
        # hash-gated cache (metadata_.ocr_text + ocr_source_hash) skips Tesseract on
        # an unchanged file; the delta is {} on any skip. Source hash honours T7
        # (content_hash when computed, else the always-present quick_hash).
        if library is not None and library.ocr_enabled:
            from filearr.tasks.ocr_run import ocr_metadata

            meta.update(
                ocr_metadata(
                    item.path,
                    file_category=item.file_category,
                    meta=meta,
                    prior_meta=item.metadata_,
                    source_hash=item.content_hash or item.quick_hash,
                    settings=settings,
                )
            )
        # P3-T13: archive member listing, keyed off EXTENSION (independent of the
        # media_type bucket -- zip/tar land in ``other``, cbz in ``document``). It
        # is a SEPARATE pass with the same guard-first, hostile-input discipline:
        # a listing failure records ``_extract_error`` but never discards the
        # already-parsed metadata, and a ratio-bomb archive is rejected by the
        # reused decompression guard BEFORE a member is enumerated. Member names
        # are untrusted STRINGS (stored verbatim, never touched as fs paths).
        archive_ok = False
        if is_archive(item.path):
            try:
                arc = list_archive_members(
                    item.path,
                    max_members=settings.archive_max_members,
                    max_stored=settings.archive_members_stored,
                    index_chars=settings.archive_members_index_chars,
                    scan_max_bytes=settings.archive_scan_max_bytes,
                    decompressed_max=settings.doc_decompressed_max,
                    ratio_limit=settings.doc_decompression_ratio,
                    ratio_min_bytes=settings.doc_decompression_ratio_min_bytes,
                )
                meta.update(arc)
                # An archive IS supported once listed: drop a document extractor's
                # ``unsupported`` marker (a cbz has no property parser) so the file
                # stops looking unhandled.
                meta.pop("unsupported", None)
                archive_ok = True
            except ArchiveError as exc:
                meta["_extract_error"] = str(exc)
                extract_failed = True

        # P4-T2: validate the extractor output against its file_category profile
        # BEFORE it reaches metadata_. This catches a future extractor regression
        # the coercers can't (e.g. a declared-int field handed a non-numeric
        # string) without ever failing the job. Only the INVALID fields are
        # dropped from ``meta``; valid fields and unregistered/ad-hoc keys
        # (extra="allow") still merge. A compact ``_validation_errors`` list
        # ([{field, reason}], capped) is recorded in metadata_ so the failure is
        # visible — distinct from ``_extract_error`` (an extraction failure);
        # validation never corrupts metadata_ with the rejected value.
        if meta:
            violations = validate_metadata(item.file_category, meta)
            if violations:
                invalid = {v.field for v in violations}
                meta = {k: val for k, val in meta.items() if k not in invalid}
                meta["_validation_errors"] = [
                    {"field": v.field, "reason": v.msg} for v in violations
                ][:20]

        # Merge extracted facts into metadata, then set the TYPED columns from
        # COERCED values only. ``coerce_year``/``coerce_str`` guarantee an
        # int|None / str|None so unvalidated tag data can never reach item.year /
        # item.title as an array or a non-numeric string and blow up at
        # session.commit() with a psycopg CannotCoerce (class B live incident).
        # When a typed parse yields nothing the raw value still lives on in
        # ``metadata`` (the extractor put it there) — we only drop it from the
        # typed column, never from the record.
        # P4-T8: record an attributed audit row ONLY when the extractor actually
        # changed metadata_. ``meta`` only adds/overwrites keys, so the change set
        # is exactly the keys whose new value differs from the previous one; a
        # byte-identical steady-state rescan yields an empty set and NO version
        # row (bounded audit growth — the P4-T9 purge handles the rest).
        prev_metadata = dict(item.metadata_)
        changed = {k: v for k, v in meta.items() if prev_metadata.get(k) != v}
        if changed:
            # W8-B: attribute the audit row to the item's taxonomy category
            # (``extract:<file_category>``), the successor to the old
            # ``extract:<media_type>`` provenance tag. NULL category (unclassified
            # row) degrades to ``extract:other``.
            source = f"extract:{item.file_category or 'other'}"
            session.add(
                ItemVersion(
                    item_id=item.id,
                    actor=source,
                    patch=changed,
                    source=source,
                )
            )

        # Follow-up (recovered-file cleanup): drop stale error sentinels left by a
        # PRIOR failed/invalid extract so a recovered file stops surfacing in the
        # error report. The two sentinels are cleared INDEPENDENTLY because they
        # mark different, orthogonal failure modes:
        #   * ``_extract_error`` — extraction/parse (or archive listing) failed. It
        #     is the ONLY key the error surface counts (errors.extract_error_count /
        #     failing_items use ``metadata ? '_extract_error'``), so it must be
        #     dropped the moment THIS run's extraction succeeds — even if validation
        #     then flags a field — or a file that now parses would wrongly linger in
        #     the error count. (The old coupled logic kept it whenever validation
        #     failed, so an extract-success + validation-fail run never left the
        #     surface; this is that gap's fix.)
        #   * ``_validation_errors`` — THIS run's output had profile-invalid fields.
        #     A run WITH violations already carries the fresh list in ``meta`` (it
        #     overwrites the old one on merge); a run with NONE clears the stale
        #     marker. Only cleared on a run whose extraction did not hard-fail — a
        #     still-erroring run keeps its prior validation sentinel untouched.
        # A run whose extractor still failed keeps ``_extract_error`` (refreshed with
        # the new message via the merge above, never duplicated — JSONB is a dict).
        merged = {**item.metadata_, **meta}
        ran_extractor = extractor is not None or archive_ok
        if ran_extractor and not extract_failed:
            merged.pop("_extract_error", None)
            if "_validation_errors" not in meta:
                merged.pop("_validation_errors", None)
        item.metadata_ = merged
        if not item.title:
            title = coerce_str(meta.get("title"))
            if title:
                item.title = title
        if not item.year:
            year = coerce_year(meta.get("year"))
            if year:
                item.year = year

        try:
            await session.commit()
        except Exception as exc:
            # Belt-and-braces (class B): a DB-layer failure while persisting
            # parsed data must NOT fail the job — extract jobs fail only on infra
            # errors (DB down etc.). Roll back every parsed change, re-load the
            # row, and retry the commit with ONLY a sanitized ``_extract_error``
            # recorded. The rolled-back state means quick_hash stays NULL, so the
            # existing null-quick_hash self-heal will also requeue this item. If
            # THIS commit also fails it is a genuine infra fault — let it raise.
            await session.rollback()
            item = (
                await session.execute(select(Item).where(Item.id == item_id))
            ).scalar_one_or_none()
            if item is None:
                return
            item.metadata_ = {**item.metadata_, "_extract_error": sanitize_error(exc)}
            extract_failed = True
            await session.commit()

        # T11 best-effort per-run attribution: if this extract failed AND it was
        # enqueued by a known scan, bump that ScanRun's error counter with a SINGLE
        # atomic JSONB update (jsonb_set on a read-modify-write done entirely in
        # SQL, so concurrent extract workers can't lose increments -- Postgres
        # serialises the row update). Authoritative counts still come from the
        # live library-wide GIN query (filearr.errors.extract_error_count); this
        # counter is a cheap, race-free convenience only, hence best-effort (a
        # missing/finished run simply matches zero rows and is a no-op).
        if extract_failed and scan_run_id:
            await session.execute(
                text(
                    "UPDATE scan_runs SET stats = jsonb_set(stats, '{extract_errors}', "
                    "to_jsonb(COALESCE((stats->>'extract_errors')::int, 0) + 1)) "
                    "WHERE id = :rid"
                ),
                {"rid": scan_run_id},
            )
            await session.commit()

    from filearr.tasks.index_sync import sync_items

    await sync_items.defer_async(item_ids=[item_id])

    # P3-T8: when semantic search is enabled, defer the LOWEST-priority embed
    # stage AFTER the commit above (invariant 5 — never race an uncommitted row).
    # Skipped when the extract failed (nothing worth embedding) or semantic search
    # is off (zero cost by default). The embed task re-syncs the item once the
    # vector lands, so build_doc can attach _vectors.
    if settings.semantic_enabled and not extract_failed:
        await proc_app.configure_task(
            "filearr.tasks.embed.embed_item",
            queue=settings.queue_embed,
            priority=settings.embed_priority,
        ).defer_async(item_id=item_id)

    # S12/P12 slice 1: thumbnail ride-along. Deferred AFTER the extract commit
    # (invariant 5 -- never race an uncommitted row) so it rides the SAME staging
    # discipline as the extract wave: by the time this runs the file's hashes are
    # committed, so ``thumb_item`` keys the content-addressed cache correctly.
    # Skipped when thumbnails are off, the extract failed (no source worth
    # thumbnailing), or the category is not thumbnailable in slice 1 (video is
    # slice 2; dev/archive/system/other get a client placeholder). The GRID tier
    # is pregenerated here; the preview tier stays lazy (serve-path on first miss).
    if settings.thumbs_enabled and not extract_failed:
        from filearr.tasks.thumbs import is_thumbnailable

        if is_thumbnailable(item_file_category, item_rel_path):
            # Best-effort enqueue: a thumbnail is a DISPOSABLE derived artifact
            # (invariant 1), so a transient enqueue failure must never fail an
            # extract that already committed + indexed -- the item still gets a
            # thumbnail later (lazy serve-path generation, or the next rescan).
            # This is deliberately more forgiving than the sync_items/embed
            # defers above, which are load-bearing for searchability.
            try:
                await proc_app.configure_task(
                    "filearr.tasks.thumbs.thumb_item",
                    queue=settings.queue_thumbnail,
                    priority=settings.thumbs_priority,
                ).defer_async(item_id=item_id)
            except Exception as exc:  # noqa: BLE001 - best-effort side effect
                log.warning("thumbnail enqueue failed for %s: %s", item_id, exc)
