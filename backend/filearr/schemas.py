"""Pydantic API schemas."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from filearr.models import HashPolicy


class LibraryIn(BaseModel):
    name: str
    root_path: str
    enabled_types: list[str] = Field(default_factory=list)  # empty = all
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)
    # P2-T3/T5 indexing controls. ``enabled_presets`` holds preset-bundle
    # names plus ``-name`` opt-out sentinels for default-on bundles; empty =
    # "no explicit config" (default-on bundles like hidden_dotfiles still apply,
    # resolved at scan time). ``enabled_extension_groups`` narrows a MediaType
    # to the union of the listed groups' extensions (R5).
    enabled_presets: list[str] = Field(default_factory=list)
    enabled_extension_groups: list[str] = Field(default_factory=list)
    scan_cron: str | None = None
    watch_mode: bool = False
    # T7 hash policy. 'auto' (default): network root -> quick_only, local -> full.
    # 'full': always full-hash (up to ceiling). 'quick_only': never full-hash.
    hash_policy: HashPolicy = HashPolicy.auto
    # Per-library override of the global full-hash size ceiling (bytes). Must be a
    # positive integer when set; null falls back to FILEARR_SCAN_HASH_FULL_MAX_BYTES.
    hash_full_max_bytes: int | None = Field(default=None, gt=0)
    # Source-system path prefix for translating container paths back to native
    # paths (e.g. '/mnt/user/media' or a UNC prefix). Optional.
    native_prefix: str | None = None
    # USER-facing network location of the library root (UI-T12): a UNC path
    # (\\tower\media), an smb:// URL, or a local mount (/Volumes/media). Drives
    # the UI open-location links + copy-path display. DISTINCT from native_prefix.
    share_prefix: str | None = None
    # P3-T6 (R4): opt this library into the CPU-costly Tesseract OCR pass.
    # Global default OFF; per-library toggle, default false.
    ocr_enabled: bool = False
    # P3-T11 (R5, CWE-1230): expose extracted GPS/location metadata for this
    # library. Default false = GPS stripped from projection + API (privacy-safe).
    expose_gps: bool = False


class LibraryUpdate(BaseModel):
    """Partial library update (PATCH). Absent fields are left untouched;
    scan_cron/watch_mode/root_path edits are re-validated at the API layer."""

    name: str | None = None
    root_path: str | None = None
    enabled_types: list[str] | None = None
    include_globs: list[str] | None = None
    exclude_globs: list[str] | None = None
    enabled_presets: list[str] | None = None
    enabled_extension_groups: list[str] | None = None
    scan_cron: str | None = None
    watch_mode: bool | None = None
    hash_policy: HashPolicy | None = None
    hash_full_max_bytes: int | None = Field(default=None, gt=0)
    native_prefix: str | None = None
    share_prefix: str | None = None
    ocr_enabled: bool | None = None
    expose_gps: bool | None = None
    enabled: bool | None = None


class LastScan(BaseModel):
    """Most-recent ScanRun for a library (FIX-10).

    Sourced per-library directly from ``scan_runs`` (a DISTINCT ON query in
    ``GET /libraries``), so it survives worker/process restarts and redeploys and
    is NOT filtered out of the capped, global ``GET /scans`` feed. Terminal
    non-``finished`` states (``failed``/``stopped``/``cancelled``) are included so
    the Admin UI shows a failed last scan rather than "never ran"; the counts are
    lifted from ``ScanRun.stats`` when present (cheap -- already loaded)."""

    started_at: datetime
    finished_at: datetime | None = None
    status: str
    seen: int | None = None
    new: int | None = None
    changed: int | None = None
    missing: int | None = None


class LibraryOut(LibraryIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    enabled: bool
    created_at: datetime
    # P5-T4: non-null on a library whose content is owned by a remote agent
    # (replicated in, never centrally scanned). Lets the UI/API distinguish an
    # agent-owned library from a locally-scanned one (its scan controls are
    # rejected server-side). NULL for an ordinary central-scanned library.
    source_agent_id: uuid.UUID | None = None
    # FIX-10: most-recent scan pulled straight from scan_runs (survives redeploy).
    last_scan: LastScan | None = None
    # OPS-T7: the EFFECTIVE user-facing share prefix + where it came from.
    # ``share_prefix`` above stays the raw manual override (NULL = unset); these
    # two are computed at read time (filearr.share_map.effective_library_share):
    # a manual prefix wins ("manual"); else the deploy mount map covering the
    # library root supplies one ("mount-map"); else none. Auto-populated so the
    # operator never hand-maintains share_prefix, and stays live across remaps.
    share_prefix_effective: str | None = None
    share_prefix_source: Literal["manual", "mount-map", "none"] = "none"
    # UI-T15: the Windows-UNC counterpart of ``share_prefix_effective`` (or None
    # when the location has no UNC form: any non-SMB scheme or a POSIX mount).
    # ``share_prefix_effective`` stays the URL-ish form; clients pick per the
    # viewer's OS (see frontend lib/osFormat.ts).
    share_unc_effective: str | None = None


class PresetOut(BaseModel):
    """One preset bundle (P2-T5). ``patterns`` are gitignore-style exclude lines;
    ``default_enabled`` marks a shipped-on bundle (only ``hidden_dotfiles`` today);
    ``caveat`` is surfaced in the Admin UI next to the toggle."""

    name: str
    label: str
    patterns: list[str]
    default_enabled: bool
    caveat: str | None = None


class ExtensionGroupOut(BaseModel):
    """One extension group (P2-T5): a set of extensions refining one MediaType."""

    name: str
    label: str
    media_type: str
    extensions: list[str]


class PresetsResponse(BaseModel):
    """``GET /api/v1/presets`` payload: all bundles + all extension groups."""

    presets: list[PresetOut]
    extension_groups: list[ExtensionGroupOut]
class ScanPathIn(BaseModel):
    """Create a scan_paths row (P2-T6 hot folder). ``rel_path`` is relative to
    the library root ('' = whole library); ``scan_cron``/``watch_mode`` are
    NULL-inherits-from-library overrides. rel_path is normalized + traversal-
    checked at the API layer (security)."""

    rel_path: str = ""
    scan_cron: str | None = None
    watch_mode: bool | None = None
    enabled: bool = True


class ScanPathUpdate(BaseModel):
    """Partial update of a scan_paths row. Absent fields untouched; explicit null
    clears (NULL-inherits). Validated with ``model_fields_set`` discipline."""

    rel_path: str | None = None
    scan_cron: str | None = None
    watch_mode: bool | None = None
    enabled: bool | None = None


class ScanPathOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    library_id: uuid.UUID
    rel_path: str
    scan_cron: str | None
    watch_mode: bool | None
    enabled: bool
    created_at: datetime


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    library_id: uuid.UUID
    media_type: str
    status: str
    path: str
    rel_path: str
    native_path: str | None = None  # native_prefix + rel_path, when configured
    # Library context for UI-T12 breadcrumbs / open-location links. Composed
    # server-side (the item row alone has only library_id).
    library_name: str | None = None
    library_share_prefix: str | None = None
    # UI-T15: Windows-UNC counterpart of ``library_share_prefix`` (None when the
    # location has no UNC form). The UI appends rel_path to whichever the viewer's
    # OS prefers; API consumers pick the field their target system needs.
    library_share_unc: str | None = None
    filename: str
    extension: str | None
    size: int
    mtime: datetime
    # Content-identity hashes (P4-T11: the Raw tab surfaces every stored column).
    quick_hash: str | None = None
    content_hash: str | None = None
    title: str | None
    year: int | None
    external_ids: dict[str, Any]
    # ``metadata`` (extracted) and ``user_metadata`` (edits) stay SEPARATE and
    # UNMERGED — the API never exposes only the effective overlay (invariant 2).
    # validation reads the ORM attribute `metadata_` (SQLAlchemy reserves
    # `.metadata` for its registry!); serialization emits "metadata".
    metadata_: dict[str, Any] = Field(
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    user_metadata: dict[str, Any]
    tags: list[str]
    # Lifecycle columns, also surfaced verbatim by the Raw tab.
    first_seen: datetime
    last_seen: datetime
    deleted_at: datetime | None = None
    sidecar_of: uuid.UUID | None = None
    # P4-T7 provenance columns (also surfaced verbatim by the Raw tab). All
    # nullable/v1-inert: the two agent columns stay NULL under local-only
    # scanning; policy_version carries the library scan-config fingerprint at last
    # extract.
    source_agent_id: uuid.UUID | None = None
    replication_seq: int | None = None
    policy_version: str | None = None
    # P10-T3: the instant an agent last confirmed this agent-hosted item exists /
    # is unchanged (NULL = never verified). Drives the item-detail "last verified"
    # freshness line for agent-owned items.
    last_verified_at: datetime | None = None
    # P10-T11/T12: the effective network-open location for this item and where it
    # was resolved from, computed at display time via the frozen precedence
    # (agent hint > agent_share_maps mapping > library share_prefix). Both NULL
    # when no location applies (no fabricated link). ``share_source`` is one of
    # "agent_hint" | "mapping" | "library". FROZEN cross-agent contract (the UI
    # renders these) — do not rename.
    share_url: str | None = None
    share_source: str | None = None

class ItemPatch(BaseModel):
    """Partial update. Absent = untouched, explicit null = clear, arrays replace."""

    title: str | None = None
    year: int | None = None
    tags: list[str] | None = None
    user_metadata: dict[str, Any] | None = None
    external_ids: dict[str, Any] | None = None


class MetadataProfileFieldOut(BaseModel):
    """One field's declared shape within a profile (P4-T1). Mirrors the stored
    ``metadata_profiles.schema`` entry: type + display/faceting hints."""

    type: str
    label: str
    required: bool = False
    facetable: bool = False
    sortable: bool = False


class MetadataProfileOut(BaseModel):
    """``GET /api/v1/metadata-profiles`` row: a code-shipped, MediaType-keyed
    schema (P4-T1). ``fields`` is the FieldSpec projection keyed by field name;
    read-only (no POST/PATCH/DELETE — profiles are code-owned)."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    media_type: str
    version: int
    created_at: datetime
    # The stored ``schema`` JSONB, exposed as ``fields`` (``schema`` shadows a
    # pydantic BaseModel helper; ``fields`` is the clearer public name).
    fields: dict[str, MetadataProfileFieldOut] = Field(
        validation_alias=AliasChoices("fields", "schema_", "schema"),
    )


class CustomFieldIn(BaseModel):
    """Create an admin-defined custom field (P4-T3, admin scope).

    ``name`` is normalised + reserved-collision-checked at the API layer
    (``custom_fields.normalize_field_name``); ``data_type`` must be in the table
    vocabulary (``custom_fields.CUSTOM_FIELD_TYPES``). ``library_ids`` empty =
    all libraries (R1); ``applies_to`` empty = all media types. ``required`` is a
    display-only hint in v1 (R3 — no write-path enforcement)."""

    name: str
    label: str
    data_type: str
    select_options: list[str] | None = None
    applies_to: list[str] = Field(default_factory=list)
    library_ids: list[uuid.UUID] = Field(default_factory=list)
    facetable: bool = False
    sortable: bool = False
    required: bool = False


class CustomFieldUpdate(BaseModel):
    """Partial update (PATCH, P4-T3). ``label`` / applicability
    (``applies_to`` / ``library_ids``) / ``select_options`` / ``facetable`` /
    ``sortable`` / ``required`` are mutable. ``name`` and ``data_type`` are
    IMMUTABLE — they are present here ONLY so an attempted edit is caught and
    rejected with a clear 422 (a rename/retype would orphan or misinterpret
    existing ``user_metadata`` values), never silently dropped as unknown."""

    label: str | None = None
    select_options: list[str] | None = None
    applies_to: list[str] | None = None
    library_ids: list[uuid.UUID] | None = None
    facetable: bool | None = None
    sortable: bool | None = None
    required: bool | None = None
    name: str | None = None       # immutable — rejected if present
    data_type: str | None = None  # immutable — rejected if present


class CustomFieldOut(BaseModel):
    """One ``custom_fields`` row (P4-T3)."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    label: str
    data_type: str
    select_options: list[str] | None = None
    applies_to: list[str]
    library_ids: list[uuid.UUID]
    facetable: bool
    sortable: bool
    required: bool
    created_at: datetime


class SearchHit(BaseModel):
    id: str
    media_type: str
    title: str | None = None
    filename: str | None = None
    path: str | None = None
    year: int | None = None
    tags: list[str] = Field(default_factory=list)
    extension: str | None = None
    size: int | None = None


class SearchResponse(BaseModel):
    hits: list[dict[str, Any]]
    total: int
    facets: dict[str, dict[str, int]] = Field(default_factory=dict)
    # P3-T4: per-numeric-facet min/max as reported by Meili ``facetStats`` (keyed
    # by field name -> {"min": float, "max": float}). Drives the size/mtime range
    # sliders; empty when the engine returns no stats (e.g. no matching docs).
    facet_stats: dict[str, dict[str, float]] = Field(default_factory=dict)
    next_cursor: str | None = None


class ScanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    library_id: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    status: str
    stats: dict[str, Any]


class FailingItem(BaseModel):
    """One item that failed metadata extraction (T11). ``error`` is sanitized."""

    id: uuid.UUID
    rel_path: str
    error: str


class FailedJob(BaseModel):
    """A recently-failed Procrastinate job (T11, read-only). ``error`` is always
    null on procrastinate 3.9 (no per-job error text stored in the DB)."""

    id: str
    queue: str
    task: str
    status: str
    attempts: int | None = None
    retry_cap: int | None = None  # FIX-12: genuine-failure retry budget for attempts/cap
    scheduled_at: str | None = None
    attempted_at: str | None = None
    error: str | None = None


class FailedJobPage(BaseModel):
    """Paginated failed-Procrastinate-jobs response (FIX-8). ``total`` is the full
    failed-row count so the UI can render a real pager; ``items`` is the requested
    page (capped at 100 server-side)."""

    items: list[FailedJob]
    total: int
    limit: int
    offset: int


class TreeFolder(BaseModel):
    """One immediate child folder in a browse listing (UI-T12). ``item_count`` is
    the number of browsable (non-sidecar, non-trashed) items anywhere beneath the
    folder subtree."""

    name: str
    item_count: int


class TreeItem(BaseModel):
    """One file whose containing directory IS the browsed path (search-hit-like).
    Sidecars are never listed (they follow their parent)."""

    id: uuid.UUID
    rel_path: str
    filename: str
    media_type: str
    size: int
    title: str | None = None
    year: int | None = None


class TreeResponse(BaseModel):
    """``GET /libraries/{id}/tree`` payload: the immediate child folders plus the
    files directly in ``path`` (paginated). ``path`` is the normalized rel_path
    that was browsed ('' = library root)."""

    library_id: uuid.UUID
    library_name: str
    path: str
    folders: list[TreeFolder]
    folders_total: int
    folders_offset: int
    items: list[TreeItem]
    total_items: int


# --------------------------------------------------------------------------- #
# P3-T10 — duplicate awareness (copy counts + copy listing)                   #
# --------------------------------------------------------------------------- #
class ItemCopy(BaseModel):
    """One OTHER copy of an item (same content). ``native_path`` resolves through
    the owning library's ``native_prefix`` (invariant 3) for the copy-path action;
    it is null when the library maps no native prefix (the container ``path`` is
    then the copy-path fallback)."""

    id: uuid.UUID
    library_id: uuid.UUID
    library_name: str | None = None
    rel_path: str
    path: str
    native_path: str | None = None
    size: int
    last_seen: datetime


class CopiesResponse(BaseModel):
    """``GET /items/{id}/copies``. ``count`` is the FULL group size INCLUDING this
    item (so the badge reads "N copies"); ``copies`` lists the OTHER members only
    (self excluded), capped at ``COPIES_CAP``. ``match`` records which grouping
    key identified the copies (``content_hash`` or the ``quick_hash`` + ``size``
    fallback when no content hash exists), or ``none`` when the item has no usable
    hash and thus no copies."""

    id: uuid.UUID
    count: int
    match: str
    capped: bool
    copies: list[ItemCopy] = Field(default_factory=list)


class CopyCountsRequest(BaseModel):
    """``POST /items/copy-counts`` body: the search-result item ids to badge in ONE
    round trip. Capped at 200 (a search page never shows more) so the single
    grouped aggregate stays cheap and un-abusable."""

    ids: list[uuid.UUID] = Field(default_factory=list, max_length=200)


# --------------------------------------------------------------------------- #
# P3-T14 — timeline (date histogram over mtime)                               #
# --------------------------------------------------------------------------- #
class TimelineBucket(BaseModel):
    """One histogram bar. ``start_epoch``/``end_epoch`` are the half-open
    [start, end) mtime window (epoch seconds) the bar covers, so clicking it maps
    to ``mtime_gte=start_epoch`` + ``mtime_lte=end_epoch - 1`` on ``/search``.
    ``start`` is the ISO-8601 bucket start for display/labels."""

    start: datetime
    start_epoch: int
    end_epoch: int
    count: int


class TimelineResponse(BaseModel):
    """``GET /stats/timeline``. ``buckets`` are the month/year bars (ascending);
    ``invalid_count`` is the number of items whose mtime is more than 48h in the
    future (FIX-3 suspect timestamps), surfaced as a SEPARATE bar. Clicking that
    bar filters ``mtime_gte=invalid_mtime_gte`` (i.e. mtime strictly beyond the
    48h future-skew window)."""

    bucket: str
    library: uuid.UUID | None = None
    buckets: list[TimelineBucket] = Field(default_factory=list)
    invalid_count: int = 0
    invalid_mtime_gte: int
