"""Typed metadata profiles (Phase 4, roadmap §7 — P4-T1 / P4-T2).

**Inert scaffolding.** Only tests import this module today; nothing in the
runtime (``extract.py`` / ``search.py`` / ``api``) wires it in yet. It ships the
*real* per-``MediaType`` field vocabularies (derived from what each extractor
actually emits — see the module docstrings in ``filearr/tasks/*.py``) plus the
*pure*, unit-testable validation logic that P4-T2 will call from
``extract_item()`` and P4-T4 (via ``custom_fields``) will call from the PATCH
path.

Design (research brief §6.1, DSpace/Paperless precedent):
- A **profile** is a code-shipped, versioned schema keyed by ``MediaType`` that
  describes the well-known fields an extractor emits. Profiles validate the
  ``metadata_`` (extractor-owned) side of invariant 2 — they are the DSpace
  ``dc``/``dcterms`` "protected schema" analogue: admin-*visible*, never
  admin-*editable*. Custom fields (``custom_fields.py``) are the freeform
  ``user_metadata`` "local namespace" side.
- Validation lives in **one** shared function (``validate_metadata`` /
  ``build_validator``) reused by every write path (Paperless #7361 lesson:
  never duplicate validation per endpoint).
- Unregistered keys **pass through** (``extra="allow"``): a profile only
  validates the keys it declares owning, so ad-hoc/future keys are never
  rejected — matching today's "any key, any value" JSONB-bag behaviour.

Architect rulings baked in:
- **R2** — a profile ``version`` bump triggers nothing automatic; a rescan is
  the existing mechanism that refreshes old items' ``metadata_``.
- **R3** — ``FieldSpec.required`` is a display-only hint in v1 (no API
  enforcement here).
- **R4** — validation is pydantic-v2 dynamic models (``create_model``), NOT
  ``pg_jsonschema`` (unverified license; native Postgres extension rejected).
- **R5** — new extractor *families* use a dotted namespace prefix
  (``exif.``, ``archive.`` …) via :func:`is_namespaced` / :func:`family_of`.
  Existing flat keys (T1/T6) are grandfathered. Phase-3 P3-T11 (EXIF deep
  extraction) MUST emit ``exif.*`` keys, not new flat ``camera``-shaped keys.

Coverage gaps (flagged, intentional — see the FieldSpec comments below):
``video.audio_tracks`` / ``video.subtitle_tracks`` / ``audiobook.chapters`` are
arrays of objects and ``model3d.bbox`` is an array of floats; none map cleanly
onto a scalar :class:`FieldSpec` ``data_type``. They are modelled with the
generic ``string_list`` (= "a JSON array", element typing not enforced in v1).
A dedicated ``object_list``/``struct`` type is deferred to the implementing task.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from filearr.models import MediaType

# FieldSpec.data_type vocabulary. Deliberately small and scalar-first; the two
# array/structured cases in the real extractor output are folded into
# ``string_list`` (see the module docstring's coverage-gap note).
DATA_TYPES = frozenset(
    {"string", "integer", "float", "boolean", "datetime", "string_list"}
)

# data_type -> the python/pydantic annotation used to build a validator. Every
# field is Optional (profiles are sparse by design — a missing key is valid).
#   datetime    -> str  : extractors emit heterogeneous *raw* date strings
#                         (EXIF "2021:01:01 ..", raw PDF date, ISO); v1 does not
#                         parse/normalise them, only records the display intent.
#   string_list -> list : a JSON array; element typing is not enforced in v1
#                         (covers list[str], list[float], and list[dict] alike).
_PY_TYPE: dict[str, Any] = {
    "string": str,
    "integer": int,
    "float": float,
    "boolean": bool,
    "datetime": str,
    "string_list": list,
}

# The extractor failure sentinel (``extract.py``). NOT a data field — it must
# never be treated as a profile-owned key nor flagged as unknown.
EXTRACT_ERROR_KEY = "_extract_error"


@dataclass(frozen=True)
class FieldSpec:
    """One well-known metadata field a profile declares ownership of.

    ``facetable``/``sortable`` drive the Meili projection (brief §6.4): they are
    the documented source of truth for which ``build_doc()`` lines exist.
    ``required`` is a display-only hint in v1 (R3) — no write path enforces it.
    """

    name: str
    data_type: str
    label: str
    facetable: bool = False
    sortable: bool = False
    required: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if self.data_type not in DATA_TYPES:
            raise ValueError(
                f"FieldSpec {self.name!r}: unknown data_type {self.data_type!r} "
                f"(allowed: {sorted(DATA_TYPES)})"
            )


# --- Real field vocabularies (derived from the live extractors) -------------
# audio: filearr/tasks/extract.py::extract_audio (tinytag)
_AUDIO_FIELDS: list[FieldSpec] = [
    FieldSpec("title", "string", "Title"),
    FieldSpec("artist", "string", "Artist", facetable=True),
    FieldSpec("album", "string", "Album", facetable=True),
    FieldSpec("genre", "string", "Genre", facetable=True),
    FieldSpec("year", "integer", "Year", facetable=True, sortable=True),
    FieldSpec("duration", "float", "Duration (s)", sortable=True),
    FieldSpec("bitrate", "float", "Bitrate (kbps)", sortable=True),
    FieldSpec("samplerate", "integer", "Sample rate (Hz)", sortable=True),
    FieldSpec("channels", "integer", "Channels", facetable=True),
]

# audiobook: audio fields + filearr/tasks/audiobook.py chapters
_AUDIOBOOK_FIELDS: list[FieldSpec] = [
    *_AUDIO_FIELDS,
    # coverage gap: list[dict] {title, start} -> generic string_list (see docstring)
    FieldSpec("chapters", "string_list", "Chapters"),
    FieldSpec("chapter_count", "integer", "Chapter count", sortable=True),
]

# image: filearr/tasks/extract.py::extract_image (Pillow)
_IMAGE_FIELDS: list[FieldSpec] = [
    FieldSpec("width", "integer", "Width (px)", sortable=True),
    FieldSpec("height", "integer", "Height (px)", sortable=True),
    FieldSpec("format", "string", "Format", facetable=True),
    FieldSpec("mode", "string", "Colour mode", facetable=True),
    FieldSpec("camera", "string", "Camera", facetable=True),
    FieldSpec("taken_at", "datetime", "Taken at", sortable=True),
]

# video: guessit filename parse + filearr/tasks/ffprobe.py::extract_video_tech
_VIDEO_FIELDS: list[FieldSpec] = [
    FieldSpec("title", "string", "Title"),
    FieldSpec("year", "integer", "Year", facetable=True, sortable=True),
    FieldSpec("season", "integer", "Season", facetable=True),
    FieldSpec("episode", "integer", "Episode", sortable=True),
    FieldSpec("container", "string", "Container", facetable=True),
    FieldSpec("duration", "float", "Duration (s)", sortable=True),
    FieldSpec("bitrate", "integer", "Bitrate (bit/s)", sortable=True),
    FieldSpec("video_codec", "string", "Video codec", facetable=True),
    FieldSpec("width", "integer", "Width (px)", sortable=True),
    FieldSpec("height", "integer", "Height (px)", sortable=True),
    FieldSpec("resolution", "string", "Resolution", facetable=True),
    FieldSpec("frame_rate", "float", "Frame rate (fps)", sortable=True),
    FieldSpec("hdr", "boolean", "HDR", facetable=True),
    FieldSpec("hdr_format", "string", "HDR format", facetable=True),
    FieldSpec("color_primaries", "string", "Colour primaries"),
    FieldSpec("color_transfer", "string", "Colour transfer"),
    FieldSpec("audio_codec", "string", "Audio codec", facetable=True),
    # coverage gap: list[dict] {codec, channels, language, ...} -> string_list
    FieldSpec("audio_tracks", "string_list", "Audio tracks"),
    # coverage gap: list[dict] {codec, language, forced, ...} -> string_list
    FieldSpec("subtitle_tracks", "string_list", "Subtitle tracks"),
]

# model3d: filearr/tasks/model3d.py (trimesh)
_MODEL3D_FIELDS: list[FieldSpec] = [
    FieldSpec("triangles", "integer", "Triangles", sortable=True),
    FieldSpec("vertices", "integer", "Vertices", sortable=True),
    FieldSpec("mesh_count", "integer", "Mesh count", sortable=True),
    # coverage gap: list[float] [dx, dy, dz] -> generic string_list
    FieldSpec("bbox", "string_list", "Bounding box"),
    FieldSpec("bbox_volume", "float", "Bounding-box volume", sortable=True),
    FieldSpec("watertight", "boolean", "Watertight", facetable=True),
    FieldSpec("file_format", "string", "File format", facetable=True),
    FieldSpec("unsupported", "boolean", "Unsupported geometry", facetable=True),
]

# document: filearr/tasks/documents.py PDF (pypdf) + DOCX (python-docx) union
_DOCUMENT_FIELDS: list[FieldSpec] = [
    FieldSpec("pages", "integer", "Pages", sortable=True),
    FieldSpec("title", "string", "Title"),
    FieldSpec("author", "string", "Author", facetable=True),
    FieldSpec("subject", "string", "Subject"),
    FieldSpec("keywords", "string", "Keywords"),
    FieldSpec("creator", "string", "Creator"),
    FieldSpec("producer", "string", "Producer", facetable=True),
    FieldSpec("encrypted", "boolean", "Encrypted", facetable=True),
    FieldSpec("paragraphs", "integer", "Paragraphs", sortable=True),
    FieldSpec("revision", "integer", "Revision"),
    FieldSpec("created", "datetime", "Created", sortable=True),
    FieldSpec("modified", "datetime", "Modified", sortable=True),
    # documents.py emits an ``unsupported`` marker for epub/mobi/txt/csv
    FieldSpec("unsupported", "boolean", "Unsupported document", facetable=True),
]

# spreadsheet: filearr/tasks/documents.py XLSX (openpyxl)
_SPREADSHEET_FIELDS: list[FieldSpec] = [
    # genuine list[str] — the one clean string_list in the whole surface
    FieldSpec("sheets", "string_list", "Sheet names"),
    FieldSpec("sheet_count", "integer", "Sheet count", sortable=True),
    FieldSpec("title", "string", "Title"),
    FieldSpec("author", "string", "Author", facetable=True),
    FieldSpec("subject", "string", "Subject"),
    FieldSpec("created", "datetime", "Created", sortable=True),
    FieldSpec("modified", "datetime", "Modified", sortable=True),
    FieldSpec("unsupported", "boolean", "Unsupported document", facetable=True),
]


# One profile per MediaType member. ``sample`` reuses the audio vocabulary
# (extract.py maps MediaType.sample -> extract_audio); ``other`` has no
# extractor, so an empty profile (all keys pass through as unregistered).
METADATA_PROFILES: dict[MediaType, list[FieldSpec]] = {
    MediaType.audio: _AUDIO_FIELDS,
    MediaType.sample: _AUDIO_FIELDS,
    MediaType.audiobook: _AUDIOBOOK_FIELDS,
    MediaType.image: _IMAGE_FIELDS,
    MediaType.video: _VIDEO_FIELDS,
    MediaType.model3d: _MODEL3D_FIELDS,
    MediaType.document: _DOCUMENT_FIELDS,
    MediaType.spreadsheet: _SPREADSHEET_FIELDS,
    MediaType.other: [],
}

# Profile schema version (brief §6.2 ``metadata_profiles.version``). Bump on any
# field-shape change; per R2 a bump triggers nothing automatic (rescan refreshes).
PROFILE_VERSION = 1


@dataclass(frozen=True)
class FieldError:
    """One structured per-field validation failure (mirrors pydantic
    ``ValidationError.errors()`` entries so the API can emit a native 422)."""

    field: str
    msg: str
    type: str


def get_profile(media_type: MediaType) -> list[FieldSpec]:
    """Return the declared field specs for ``media_type`` (``[]`` for a type
    with no extractor-owned vocabulary, e.g. ``other``)."""
    return METADATA_PROFILES.get(media_type, [])


def build_validator(fields: list[FieldSpec], *, name: str = "ProfileModel") -> type[BaseModel]:
    """Compile a list of :class:`FieldSpec` into a pydantic model.

    Every field is Optional (sparse) and ``extra="allow"`` so unregistered keys
    pass through unvalidated — preserving the current "any key, any value" JSONB
    behaviour while still type-checking the keys a profile *does* own. This is
    the single shared validator builder reused by custom-field validation too.
    """
    definitions: dict[str, Any] = {}
    for spec in fields:
        py_type = _PY_TYPE[spec.data_type]
        definitions[spec.name] = (py_type | None, None)
    return create_model(name, __config__=ConfigDict(extra="allow"), **definitions)


@cache
def _validator_for(media_type: MediaType) -> type[BaseModel]:
    return build_validator(get_profile(media_type), name=f"{media_type.value.title()}Profile")


def validate_metadata(media_type: MediaType, payload: dict[str, Any]) -> list[FieldError]:
    """Validate an extracted ``metadata_`` payload against its profile.

    Returns a list of structured :class:`FieldError` (empty == valid). The
    ``_extract_error`` sentinel and any unregistered key pass through untouched.
    Pure: no IO, no DB. Used by P4-T2 inside ``extract_item()`` (report a
    violation via ``_extract_error``, never raise / never drop the value).
    """
    validator = _validator_for(media_type)
    try:
        validator.model_validate(payload)
    except ValidationError as exc:
        return [
            FieldError(
                field=str(e["loc"][0]) if e.get("loc") else "",
                msg=e["msg"],
                type=e["type"],
            )
            for e in exc.errors()
        ]
    return []


# --- R5 namespace helpers ---------------------------------------------------
def is_namespaced(key: str) -> bool:
    """True when ``key`` carries a dotted family prefix (``exif.camera_model``).

    New extractor families adopt this convention (R5); existing flat T1/T6 keys
    (``camera``, ``video_codec`` …) are grandfathered and return ``False``.
    """
    head, sep, tail = key.partition(".")
    return bool(sep) and bool(head) and bool(tail)


def family_of(key: str) -> str | None:
    """Return the family prefix of a namespaced key (``exif`` for
    ``exif.camera_model``), or ``None`` for a grandfathered flat key."""
    if not is_namespaced(key):
        return None
    return key.partition(".")[0]


# --- P4-T1: DB projection + startup seed ------------------------------------
def field_spec_to_dict(spec: FieldSpec) -> dict[str, Any]:
    """Project one :class:`FieldSpec` onto the ``metadata_profiles.schema`` JSON
    shape ``{type, required, facetable, sortable, label}`` (brief §6.2)."""
    return {
        "type": spec.data_type,
        "required": spec.required,
        "facetable": spec.facetable,
        "sortable": spec.sortable,
        "label": spec.label,
    }


def profile_schema(fields: list[FieldSpec]) -> dict[str, dict[str, Any]]:
    """Project a profile (list of :class:`FieldSpec`) onto the stored JSON schema
    keyed by field name — the payload the read API returns and the seed upserts."""
    return {spec.name: field_spec_to_dict(spec) for spec in fields}


async def seed_profiles_to_db(session_factory: Any = None) -> None:
    """Upsert :data:`METADATA_PROFILES` into the ``metadata_profiles`` table.

    Called at app startup (lifespan) and from ``scripts.init_db`` after
    migrations. Idempotent and code-owned: migrations only ever ADD rows or bump
    ``version`` (R2), so this upserts every code-defined profile keyed by
    ``media_type`` and records :data:`PROFILE_VERSION`. The ``ON CONFLICT`` guard
    only overwrites a row whose stored ``version`` is ``<=`` the code version, so
    a hand-bumped newer row is never silently downgraded; re-running with an
    unchanged code version leaves the row byte-identical (no dup rows, ``version``
    unchanged). ``created_at`` is never touched on update.

    ``session_factory`` defaults to ``filearr.db.SessionLocal``; tests pass a
    test-bound sessionmaker. DB imports are function-local so the module stays
    pure/import-cheap for the unit-test surface.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from filearr.db import SessionLocal
    from filearr.models import MetadataProfile

    factory = session_factory or SessionLocal
    async with factory() as session:
        for media_type, fields in METADATA_PROFILES.items():
            schema = profile_schema(fields)
            stmt = pg_insert(MetadataProfile).values(
                media_type=media_type.value,
                version=PROFILE_VERSION,
                schema_=schema,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["media_type"],
                set_={
                    MetadataProfile.version: PROFILE_VERSION,
                    MetadataProfile.schema_: schema,
                },
                where=MetadataProfile.version <= PROFILE_VERSION,
            )
            await session.execute(stmt)
        await session.commit()
