"""SQLAlchemy 2.0 typed models.

Design notes:
- Postgres 18 native uuidv7() primary keys (time-ordered, index-friendly inserts).
- sist2-style hybrid schema: typed core columns + sparse JSONB metadata bag.
- `metadata_` = extracted facts (rescans may overwrite); `user_metadata` = manual/API
  edits (never touched by scans). Effective value = user overlay on extracted.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import UserDefinedType


class Base(DeclarativeBase):
    pass


class LtreeCompat(UserDefinedType):
    """Type for columns that are ``ltree`` in production but plain ``text`` where
    the extension is unavailable (the pgserver test sandbox) — currently
    ``items.path_scope`` and ``path_grants.scope`` (migration ``d7e4c1b9f3a2``).

    Must NOT be ``Text``: the psycopg dialect renders a ``::VARCHAR`` bind cast
    for Text-typed parameters, and Postgres has no varchar→ltree assignment cast,
    so every INSERT/UPDATE writing such a column fails with 42804 on a real
    ltree deployment (invisible under the text fallback the test sandbox runs).
    A ``UserDefinedType`` renders the bare parameter, which binds as ``unknown``
    and coerces to the target column's actual type on the server — ltree in
    production, text in the sandbox. Values are plain dotted-label strings
    (``rbac.path_to_ltree`` output) in both cases; reads come back as ``str``.
    """

    cache_ok = True

    def get_col_spec(self, **kw) -> str:
        # DDL fallback (create_all bootstrap): plain TEXT. Production column
        # typing to ltree is the migration's job, never the ORM's.
        return "TEXT"


class MediaType(str, enum.Enum):
    video = "video"
    audio = "audio"
    audiobook = "audiobook"
    sample = "sample"
    image = "image"
    model3d = "model3d"
    document = "document"
    spreadsheet = "spreadsheet"
    other = "other"


class ItemStatus(str, enum.Enum):
    active = "active"
    missing = "missing"   # not seen on last scan (tombstone)
    trashed = "trashed"   # user-deleted, awaiting recycle-bin purge


class HashPolicy(str, enum.Enum):
    """Per-library content-hash policy (T7). Controls whether a scan/extract
    computes the expensive full ``content_hash`` (whole-file xxh3), which streams
    every byte and is the pain point over SMB/NFS for multi-GB video.

    * ``auto`` (default) -- decide by filesystem: a network root (SMB/NFS/FUSE-
      remote, via :func:`filearr.schedule.is_network_path`) behaves like
      ``quick_only``; a local root behaves like ``full``.
    * ``full`` -- always compute content_hash (up to the size ceiling), regardless
      of filesystem.
    * ``quick_only`` -- never compute content_hash on scan/extract; only the cheap
      quick_hash (first+last 64 KiB) is stored.

    quick_hash is ALWAYS computed regardless of policy: it is a bounded ~128 KiB
    read used for move-detection tier 1 and is cheap even over the network.
    """

    auto = "auto"
    full = "full"
    quick_only = "quick_only"


class Library(Base):
    __tablename__ = "libraries"
    __table_args__ = (
        # P5-T4: a library auto-provisioned for an agent-hosted corpus is keyed by
        # (source_agent_id, agent_library_ref) so a second replication batch reuses
        # the SAME central library instead of minting a duplicate. Partial (only
        # where source_agent_id IS NOT NULL) so the many locally-scanned libraries
        # (both columns NULL) never collide on this index.
        Index(
            "uq_libraries_source_agent_ref",
            "source_agent_id",
            "agent_library_ref",
            unique=True,
            postgresql_where=text("source_agent_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text, unique=True)
    root_path: Mapped[str] = mapped_column(Text)
    # user-selectable media type inclusion (empty = all types)
    enabled_types: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    include_globs: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    exclude_globs: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    # P2-T1 preset bundles (indexing controls). Empty '{}' = "no explicit config":
    # effective presets are resolved at scan time via presets.resolve_effective_presets
    # (union of default_enabled bundles + stored positive entries, minus '-name'
    # negative sentinels e.g. '-hidden_dotfiles'). NOT the effective set, so the
    # shipped-on default set can evolve without a data migration.
    enabled_presets: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    # P2-T3 extension-group refinement (finer than MediaType; union semantics, R5).
    enabled_extension_groups: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'")
    )
    # Native path prefix on the SOURCE system (e.g. '/mnt/user/media' on Unraid or
    # '\\\\tower\\media'). Mount paths inside this container differ from the origin
    # system; native_prefix lets the API/UI/exports translate container paths back
    # to source-system paths (same idea as *arr 'remote path mappings').
    native_prefix: Mapped[str | None] = mapped_column(Text)
    # USER-facing network location of the library root (UI-T12): what a human
    # types into a file manager to open a file -- a UNC path (\\tower\media), an
    # smb:// URL, or a local mount (/Volumes/media). DISTINCT from native_prefix
    # (source-system path for *arr-style remote mapping): share_prefix drives the
    # UI "Open via network" links + copy-path display, native_prefix drives export
    # path translation. Optional; NULL = no open-location affordance.
    share_prefix: Mapped[str | None] = mapped_column(Text)
    scan_cron: Mapped[str | None] = mapped_column(Text)
    # FIX-8 (scan-scheduling storm): the cron OCCURRENCE instant this library's
    # schedule most recently fired for. The minute tick fires a scan only for an
    # occurrence strictly newer than this and stamps the occurrence here in the
    # same commit as the enqueue, so each occurrence fires at most once even
    # across duplicate/late ticks or a worker that died mid-scan. NULL = never
    # fired (a freshly-set schedule fires only from its next occurrence, never a
    # backfilled catch-up). See schedule.due_occurrence.
    last_cron_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    watch_mode: Mapped[bool] = mapped_column(server_default=text("false"))  # local paths only
    # T7 hash policy: 'auto' (network->quick_only, local->full) | 'full' |
    # 'quick_only'. Stored as text (not a PG enum) so adding a future policy value
    # needs no ALTER TYPE; validated against HashPolicy at the API boundary.
    hash_policy: Mapped[str] = mapped_column(Text, server_default=text("'auto'"))
    # Per-library override of FILEARR_SCAN_HASH_FULL_MAX_BYTES: files at/below this
    # many bytes may be full-hashed. NULL -> fall back to the global setting.
    hash_full_max_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # P3-T6 (R4): per-library opt-in for the CPU-costly Tesseract OCR pass.
    # Global default is OFF (FILEARR_OCR_ENABLED=false); flipping this true opts a
    # library in. Mirrors the watch_mode boolean mapping.
    ocr_enabled: Mapped[bool] = mapped_column(server_default=text("false"))
    # P3-T11 (R5, CWE-1230): per-library opt-in for exposing GPS/location metadata.
    # exiftool-extracted GPS fields always land in metadata_ (extracted truth,
    # invariant 2) but are stripped from the Meili projection + public API unless
    # this is true. Default false = privacy-safe; ships with GPS extraction (R5).
    expose_gps: Mapped[bool] = mapped_column(server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    # P5-T4 distributed agents: a library whose CONTENT is owned by a remote agent
    # (replicated in via the agent outbox), not scanned by central. ``root_path``
    # is then the agent-side absolute root path (the ``library_ref`` verbatim —
    # central never opens it). Both NULL for an ordinary central-scanned library.
    # ``ON DELETE SET NULL`` (via the migration FK) so removing an agent orphans
    # its libraries rather than cascade-deleting the replicated catalog. Agent-
    # owned libraries are EXCLUDED from central scanning: the cron scheduler tick
    # filters them out and the manual-scan / watch paths reject them (422).
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    # The agent-opaque ``library_ref`` this central library materializes (the
    # agent's local library identifier / root path). Unique per source_agent_id
    # (partial unique index) so a repeat batch reuses this row.
    agent_library_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )

    items: Mapped[list["Item"]] = relationship(back_populates="library")


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        # Identity is the path RELATIVE to the library root: stable across mount
        # relocations (host bind vs in-LXC rclone mount vs future migrations).
        Index("ix_items_library_rel_path", "library_id", "rel_path", unique=True),
        # UI-T12 folder-browse: anchored LIKE 'prefix/%' scans over rel_path.
        # text_pattern_ops makes prefix matches index-served regardless of the
        # DB collation (the unique index above uses the collation-aware opclass,
        # which does not accelerate LIKE under a non-C locale).
        Index(
            "ix_items_library_rel_path_pattern",
            "library_id",
            "rel_path",
            postgresql_ops={"rel_path": "text_pattern_ops"},
        ),
        Index("ix_items_media_type", "media_type"),
        Index("ix_items_status", "status"),
        Index("ix_items_quick_hash", "quick_hash"),
        Index("ix_items_metadata", "metadata", postgresql_using="gin"),
        Index("ix_items_tags", "tags", postgresql_using="gin"),
        Index("ix_items_sidecar_of", "sidecar_of"),
        # P4-T5: GIN over the user-edit overlay (custom-field filtering) + a
        # native, no-extension structural guard that user_metadata is always a
        # JSON object (defense-in-depth; the API/ORM already enforce a dict).
        Index("ix_items_user_metadata", "user_metadata", postgresql_using="gin"),
        CheckConstraint(
            "jsonb_typeof(user_metadata) = 'object'", name="user_metadata_is_object"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    library_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType, name="media_type"))
    status: Mapped[ItemStatus] = mapped_column(
        Enum(ItemStatus, name="item_status"), server_default=ItemStatus.active.value
    )

    path: Mapped[str] = mapped_column(Text)      # absolute path as currently mounted
    rel_path: Mapped[str] = mapped_column(Text)  # path relative to library.root_path (identity)
    filename: Mapped[str] = mapped_column(Text)
    extension: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    mtime: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # xxh3: quick = first+last 64 KiB (move detection tier 1); content = full/chunked
    quick_hash: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)

    title: Mapped[str | None] = mapped_column(Text)
    year: Mapped[int | None] = mapped_column(Integer)
    external_ids: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, server_default=text("'{}'"))
    user_metadata: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))

    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # T3 sidecar association: non-primary files (.nfo, poster.jpg, -thumb.jpg,
    # *_JRSidecar.xml, ...) point at the parent media item they belong to. Self-
    # referential, nullable. ondelete=CASCADE: a sidecar has no meaning without its
    # parent, so a parent hard-purge (recycle-bin) removes orphaned sidecars too
    # (integrity > keeping stray artwork rows). Soft tombstoning is unaffected —
    # the FK only bites at real DELETE time.
    sidecar_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE"), nullable=True
    )

    # P4-T7 provenance columns (all nullable, v1-inert, phase-5-ready). The two
    # agent columns stay NULL under local-only scanning; a phase-5 distributed-
    # agent outbox/replication producer populates them later WITHOUT a migration
    # ("ship the column now, wire the producer later" — mirrors
    # ``hash_full_max_bytes``). ``policy_version`` is a short stable fingerprint of
    # the owning library's scan-relevant config, written at extract time
    # (``filearr.provenance.policy_version``); it changes when that config changes
    # and stays NULL until the item is (re)extracted.
    source_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    replication_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    # P6-T2 RBAC: the ltree-encoded ``(library, rel_path)`` scope key this item
    # lives under (``lib_<uuid.hex>.<encoded rel_path>`` — see ``filearr.rbac``).
    # Stored as text so the ORM/create_all path stays extension-free; the
    # migration types the real column ``ltree`` (with a GIST ancestor index) when
    # the extension is available, else text. NULL until a scan/backfill stamps it
    # (invariant: extractors/scans only write it on item create + rel_path move).
    path_scope: Mapped[str | None] = mapped_column(LtreeCompat(), nullable=True)

    # P10-T3 agent verification: the instant an agent last confirmed (via a
    # stat_check / rehash_check command) that this agent-hosted item still exists
    # and is unchanged. NULL = never verified. Only ever stamped for items whose
    # library is agent-owned (``libraries.source_agent_id``); a centrally-scanned
    # item keeps NULL. Written by the verify-completion reconcile path
    # (``filearr.verify``); surfaced as the item-detail "last verified" freshness
    # line. Migration ``b7e3d1f9a2c4``.
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- P10-T11 begin (agent share-location discovery) ---------------------
    # The best-effort network-share hint an agent reports for this item, stored
    # verbatim as the additive replication-event `share_hint` object
    # ({share_url, unc, share_name, host, source:"agent"}). NULL is the normal
    # case (R1: discovery is advisory; anonymous shares / permission-scoped
    # enumeration / multi-homed hosts mean most agents report nothing) and falls
    # through to the central mapping fallback (P10-T12). Set by apply_batch on the
    # upsert path only; NEVER written for a centrally-scanned item. Stored as an
    # opaque JSONB dict (not a typed column) so the wire shape stays additive and
    # versionable — a future field rides along without a migration. Migration
    # `e4a7c2f1b9d6`.
    share_hint: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # --- P10-T11 end --------------------------------------------------------

    library: Mapped[Library] = relationship(back_populates="items")
    parent: Mapped["Item | None"] = relationship(
        "Item", remote_side="Item.id", back_populates="sidecars"
    )
    sidecars: Mapped[list["Item"]] = relationship(
        "Item", back_populates="parent", cascade="all, delete-orphan"
    )

    @property
    def effective_metadata(self) -> dict:
        return {**self.metadata_, **self.user_metadata}

    @property
    def is_sidecar(self) -> bool:
        return self.sidecar_of is not None


class ItemVersion(Base):
    """Audit trail for metadata changes (P4-T8: user edits AND extractor writes).

    ``source`` discriminates the origin: ``'user'`` for an API/UI edit,
    ``'scan'`` / ``'extract:<media_type>'`` for an attributed extractor write.
    Pre-P4-T8 rows were all manual edits and backfill to ``'user'``. Only
    non-``'user'`` rows are subject to the P4-T9 retention purge; ``'user'`` rows
    are never auto-purged.
    """

    __tablename__ = "item_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    actor: Mapped[str] = mapped_column(Text)
    patch: Mapped[dict] = mapped_column(JSONB)
    # P4-T8: origin discriminator. Server default backfills pre-existing rows to
    # 'user'; extractor-sourced rows set 'scan'/'extract:<media_type>'.
    source: Mapped[str] = mapped_column(Text, server_default=text("'user'"))

    # P4-T9: composite index backing the retention purge (source != 'user' AND
    # changed_at < cutoff). Leading ``source`` lets the purge cheaply skip the
    # exempt 'user' partition.
    __table_args__ = (
        Index("ix_item_versions_source_changed_at", "source", "changed_at"),
    )


class MetadataProfile(Base):
    """Code-shipped, versioned, ``MediaType``-keyed schema describing the
    well-known fields an extractor emits (P4-T1).

    Admin-*visible*, never admin-*editable*: seeded/upserted at startup from
    :data:`filearr.profiles.METADATA_PROFILES` (mirrors how ``MediaType`` /
    ``HashPolicy`` are code-owned enums). Migrations only ever ADD rows or bump
    ``version`` (R2) — there is no admin DELETE/UPDATE endpoint for ``schema``.
    The ``schema`` JSONB mirrors the ``FieldSpec`` projection
    (``{field: {type, required, facetable, sortable, label}}``).
    """

    __tablename__ = "metadata_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    # FK-like to a MediaType enum value; UNIQUE so seed is a clean upsert target.
    media_type: Mapped[str] = mapped_column(Text, unique=True)
    # Bumped on any field-shape change (R2); a bump triggers nothing automatic.
    version: Mapped[int] = mapped_column(Integer)
    # SQLAlchemy attribute is ``schema_`` (``.schema`` collides with nothing at
    # the ORM level, but the trailing underscore mirrors ``Item.metadata_`` and
    # keeps the mapped name distinct from Table.schema / pydantic helpers).
    schema_: Mapped[dict] = mapped_column("schema", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class CustomField(Base):
    """Admin-defined custom-field definition (P4-T3 table shape).

    The **table** ships in the combined P4 migration so the schema settles in one
    revision; the admin-scope CRUD API (create/list/update/soft-delete) is P4-T3
    (pending). Values governed by a definition live ONLY in ``Item.user_metadata``
    (invariant 2 — extractors cannot write there, so a rescan never clobbers an
    admin field). ``data_type`` + ``name`` are immutable after creation (enforced
    at the API layer, not a DB trigger). ``applies_to`` / ``library_ids`` empty =
    "all media types" / "all libraries" (R1).
    """

    __tablename__ = "custom_fields"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text, unique=True)  # the user_metadata key
    label: Mapped[str] = mapped_column(Text)
    data_type: Mapped[str] = mapped_column(Text)  # string|integer|float|boolean|date|url|select
    select_options: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    applies_to: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    library_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), server_default=text("'{}'")
    )
    facetable: Mapped[bool] = mapped_column(server_default=text("false"))
    sortable: Mapped[bool] = mapped_column(server_default=text("false"))
    required: Mapped[bool] = mapped_column(server_default=text("false"))  # display-only hint (R3)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    library_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    # P2-T6 scan scope: NULL = full-library scan; a rel_path = the subtree a
    # scoped (hot-folder) scan ran against. Lets the scheduler tell a running
    # FULL scan (which a scoped defer skips behind) from a running scoped scan.
    rel_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))


class ScanPath(Base):
    """P2-T6 per-subfolder scan/watch override (brief §3.6, "hot folders").

    One row governs a subtree of a library identified by ``rel_path`` (relative
    to ``libraries.root_path``; ``''`` = the library root itself, reusing the
    ``items.rel_path`` identity convention, invariant 3). ``scan_cron`` /
    ``watch_mode`` are **NULL-inherits-from-library** overrides (mirroring T7's
    ``hash_full_max_bytes`` NULL-inherits-global pattern): a row is a pure
    override, not a full config duplicate. The scheduler defers a *scoped* scan
    for a row whose own ``scan_cron`` is due; a NULL ``scan_cron`` row adds no
    scheduling (the subtree is covered by the library's full scan). The watch
    supervisor starts an extra watcher for a row with ``watch_mode`` true.

    ``UNIQUE(library_id, rel_path)`` makes each subfolder configurable exactly
    once; the FK CASCADEs so deleting a library drops its scan_paths rows.
    """

    __tablename__ = "scan_paths"
    __table_args__ = (
        Index("ix_scan_paths_library_id", "library_id"),
        Index(
            "uq_scan_paths_library_rel_path", "library_id", "rel_path", unique=True
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE")
    )
    # Path relative to library.root_path; '' = the library root (whole library).
    rel_path: Mapped[str] = mapped_column(Text)
    # cronsim expression; NULL = inherit the library's scan_cron (no separate
    # scoped defer -- the subtree rides the library's full scan).
    scan_cron: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FIX-8 (scan-scheduling storm): per-scoped-schedule once-per-occurrence
    # marker, mirroring libraries.last_cron_fired_at. A hot folder with its own
    # cron gets the same at-most-once-per-occurrence guarantee as a library.
    last_cron_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # NULL = inherit the library's watch_mode; True = watch this subtree (local
    # paths only, re-checked per resolved absolute path).
    watch_mode: Mapped[bool | None] = mapped_column(nullable=True)
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text)
    prefix: Mapped[str] = mapped_column(Text, unique=True)   # e.g. "ck_a1b2c3"
    key_hash: Mapped[str] = mapped_column(Text)              # sha256 hex (keys are high-entropy)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{read}'"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


# --------------------------------------------------------------------------- #
# Phase 8 — Alerting (P8-T1 schema). Four tables + a fan-out junction. The     #
# pure rule/window/ssrf/signing cores live in filearr.alerts.*; these ORM      #
# rows are the durable state (channel configs with secrets encrypted at rest,  #
# rule definitions, and the match/delivery-queue records). See                 #
# docs/tasks/phase-8-alerting-tasks.md (§ Intended schema) and                 #
# docs/research/phase-8-alerting.md §8.1.                                       #
# --------------------------------------------------------------------------- #


class AlertChannel(Base):
    """A notification destination (webhook / email / apprise).

    ``config`` is a JSONB bag whose *secret* sub-fields are stored as AES-GCM
    ciphertext strings (P8-T4 / :mod:`filearr.alerts.crypto`), never plaintext —
    a stolen Postgres dump exposes no credentials without ``FILEARR_SECRET_KEY``.
    ``dispatch_locality`` (R6) is an authoritative admin choice, not
    auto-detected. ``type`` is exposed to Python as ``type_`` to avoid shadowing
    the builtin (mirrors ``Item.metadata_`` / ``MetadataProfile.schema_``).
    """

    __tablename__ = "alert_channels"
    __table_args__ = (
        CheckConstraint(
            "type IN ('webhook','email','apprise')", name="alert_channel_type_valid"
        ),
        CheckConstraint(
            "dispatch_locality IN ('central','agent')",
            name="alert_channel_dispatch_locality_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text, unique=True)
    type_: Mapped[str] = mapped_column("type", Text)  # webhook|email|apprise
    config: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    dispatch_locality: Mapped[str] = mapped_column(
        Text, server_default=text("'central'")
    )
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AlertRule(Base):
    """A file-watch (or ``is_system`` operational) alert rule — mirrors the pure
    :class:`filearr.alerts.rules.AlertRule` dataclass and the intended DDL.

    ``library_id=NULL`` = all libraries. ``group_by`` is fixed to
    ``{event_type,library_id,rule_id}`` for v1 (R1). ``threshold_*`` are only
    populated for ``is_system`` operational rules. Channels are attached via the
    :class:`AlertRuleChannel` junction, not an inline array, so channel deletes
    cascade cleanly.
    """

    __tablename__ = "alert_rules"
    __table_args__ = (
        CheckConstraint(
            "digest_window IS NULL OR digest_window IN ('hourly','daily')",
            name="alert_rule_digest_window_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    is_system: Mapped[bool] = mapped_column(server_default=text("false"))
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE"), nullable=True
    )
    path_glob: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(Text))
    hash_change_only: Mapped[bool] = mapped_column(server_default=text("false"))
    group_by: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{event_type,library_id,rule_id}'")
    )
    group_wait_s: Mapped[int] = mapped_column(Integer, server_default=text("30"))
    digest_window: Mapped[str | None] = mapped_column(Text, nullable=True)
    repeat_interval_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    threshold_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    threshold_window_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AlertRuleChannel(Base):
    """Many-to-many fan-out: which channels a rule dispatches to."""

    __tablename__ = "alert_rule_channels"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE"), primary_key=True
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_channels.id", ondelete="CASCADE"), primary_key=True
    )


class AlertEvent(Base):
    """A rule match + its delivery-queue / digest-buffer state (§3.3).

    Written only when a rule actually matches (no unconditional per-file event
    log). Undelivered rows accumulating per ``dedup_key`` *are* the digest
    buffer; ``occurred_at`` seeds group-wait, ``delivered_at`` seeds
    repeat-interval and the P8-T15 rolling-hour ceiling. ``payload`` carries the
    rendered, already-sanitized event data handed to the channel drivers.
    ``last_error`` is stored ``sanitize_error``'d + capped.
    """

    __tablename__ = "alert_events"
    __table_args__ = (
        Index(
            "ix_alert_events_pending",
            "rule_id",
            "dedup_key",
            postgresql_where=text("NOT delivered"),
        ),
        Index("ix_alert_events_rule_delivered_at", "rule_id", "delivered_at"),
        # NOTE: a partial UNIQUE index
        #   uq_alert_events_dedup_pending
        #     (rule_id, dedup_key, COALESCE(item_id, nil)) WHERE NOT delivered
        # race-proofs the P8-T5 dedup and is created by migration f3b8d2a41c5e.
        # It is intentionally migration-only (an expression index) — not declared
        # here — so create_all()-based unit tests skip it; the pipeline's
        # ON CONFLICT DO NOTHING degrades safely to the app-level dedup without it.
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="CASCADE")
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        # CASCADE (migration f3b8d2a41c5e, was SET NULL in P8-T1): a pending
        # file-event alert about a hard-deleted item is moot, and CASCADE keeps
        # the COALESCE dedup index collision-free (SET NULL would collapse a
        # group's distinct item_ids to NULL and self-collide on delete). Ops
        # alerts carry NULL item_id and are never cascaded.
        ForeignKey("items.id", ondelete="CASCADE"), nullable=True
    )
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("libraries.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(Text)
    dedup_key: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    delivered: Mapped[bool] = mapped_column(server_default=text("false"))
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivery_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SavedSearch(Base):
    """P3-T7 — a named, persisted ``/search`` query (brief §7 P1).

    Pure Postgres, never a Meili concept (invariant 1): ``params`` is the flat
    ``/search`` query bundle stored verbatim as JSONB and replayed by re-running
    the endpoint, so the row is trivially rebuild-compatible. ``owner_principal``
    is an R7 placeholder (nullable now; phase-6 RBAC enforces it). The
    ``UNIQUE(owner_principal, name)`` constraint gives per-owner name uniqueness;
    the API validates each ``params`` key against the ``/search`` signature-derived
    ``SEARCH_PARAM_NAMES`` before persisting so an unknown/renamed key is a 422.
    """

    __tablename__ = "saved_searches"
    __table_args__ = (
        Index("ix_saved_searches_owner", "owner_principal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text)
    owner_principal: Mapped[str | None] = mapped_column(Text, nullable=True)
    params: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ReportDefinition(Base):
    """P11-T5 — a saved CUSTOM report: a querydsl string + a column projection.

    The ``query`` (the DSL string) is the source of truth — parsed and compiled to
    SQL at run time (``filearr.query_sql``), never a stored SQL fragment. ``columns``
    is the ordered projection (core columns and/or ``meta.<key>``/``cf.<name>``),
    validated against a column registry on write. ``owner_principal`` is the same
    nullable R7 placeholder as ``saved_searches`` (phase-6 RBAC enforces later);
    ``UNIQUE(owner_principal, name)`` gives per-owner name uniqueness. Scheduling,
    ``report_runs`` history, and the background-export job are deferred (later
    P11-T5/T9 work) — this round is definitions + synchronous run only.
    """

    __tablename__ = "report_definitions"
    __table_args__ = (
        UniqueConstraint("owner_principal", "name", name="uq_report_definitions_owner_name"),
        Index("ix_report_definitions_owner", "owner_principal"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text)
    owner_principal: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The querydsl string (grammar source of truth), parsed+compiled on run.
    query: Mapped[str] = mapped_column(Text)
    #: Ordered column projection (core / ``meta.<key>`` / ``cf.<name>``).
    columns: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    #: Optional sort spec: a column name, ``-`` prefix = descending.
    sort: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Default export format for the UI run button.
    format: Mapped[str] = mapped_column(Text, server_default=text("'csv'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ReportExport(Base):
    """P11-T5/T11 — a background report/export JOB + its produced artifact.

    An async export streams a report result (canned or custom) to a diskguarded
    staging file under ``{config_dir}/exports`` and tracks the job lifecycle here
    (mirrors the research §9 ``report_runs`` shape). The artifact lives OUTSIDE
    any web-served static root; ``GET /exports/{id}/download`` re-checks RBAC at
    download time and streams the file. Crash handling mirrors invariant 7: a
    reconcile sweep flips a stale ``running`` row to ``failed`` (never left
    running); TTL purge deletes the file past ``expires_at`` and stamps
    ``purged_at`` while KEEPING the row (audit trail, Phase-8 posture).

    Source is EXACTLY ONE of ``report_definition_id`` (a custom report) or
    ``canned_report_key`` (a registry id) — the XOR CHECK enforces it. ``params``
    is the light canned parameterisation (``library_id``/``limit``) captured at
    enqueue so the job rebuilds the exact query.
    """

    __tablename__ = "report_exports"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','complete','failed')",
            name="report_export_status_valid",
        ),
        CheckConstraint(
            "num_nonnulls(report_definition_id, canned_report_key) = 1",
            name="report_export_source_xor",
        ),
        Index("ix_report_exports_owner", "owner_principal"),
        Index("ix_report_exports_status", "status"),
        Index(
            "ix_report_exports_expires",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL AND purged_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    report_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("report_definitions.id", ondelete="CASCADE"), nullable=True
    )
    canned_report_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The schedule that produced this export (P11-T9), when triggered by one.
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("report_schedules.id", ondelete="SET NULL"), nullable=True
    )
    triggered_by: Mapped[str] = mapped_column(Text, server_default=text("'manual'"))
    owner_principal: Mapped[str | None] = mapped_column(Text, nullable=True)
    format: Mapped[str] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Scheduled-delivery bookkeeping (P11-T9): pending|delivered|failed|none.
    delivery_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    purged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ReportSchedule(Base):
    """P11-T9 — a scheduled report delivery.

    A cron expression (evaluated by the SAME static-tick + cronsim +
    once-per-occurrence ``last_cron_fired_at`` machinery as ``scan_cron``,
    FIX-8/FIX-9) fires a background :class:`ReportExport`; on completion the
    export is delivered through a Phase-8 :class:`AlertChannel` (email attaches
    the artifact when small enough, else a link summary; webhook gets a JSON
    summary + download URL, never the file inline). ``last_cron_fired_at`` is the
    idempotency key that guarantees at most one fire per occurrence across
    duplicate/late ticks.

    Source is EXACTLY ONE of ``report_definition_id`` / ``canned_report_key``
    (XOR CHECK), like :class:`ReportExport`.
    """

    __tablename__ = "report_schedules"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(report_definition_id, canned_report_key) = 1",
            name="report_schedule_source_xor",
        ),
        Index("ix_report_schedules_owner", "owner_principal"),
        Index(
            "ix_report_schedules_enabled",
            "enabled",
            postgresql_where=text("enabled"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text)
    owner_principal: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("report_definitions.id", ondelete="CASCADE"), nullable=True
    )
    canned_report_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    params: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    format: Mapped[str] = mapped_column(Text, server_default=text("'csv'"))
    cron: Mapped[str] = mapped_column(Text)
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("alert_channels.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    last_cron_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ThumbnailManifest(Base):
    """S12/P12 slice 1 -- content-addressed WebP thumbnail cache index.

    A DISPOSABLE projection (invariant 1 applied to a derived store): the
    filesystem under ``{config_dir}/thumbnails`` holds the bytes; this table only
    indexes them so GC/staleness are cheap Postgres queries, not million-entry
    directory walks. Every row is rebuildable from the still-live source via the
    ``thumb_item`` task.

    ``cache_key`` = ``blake2b(hash:generator_version:tier)`` hex (filearr.thumbs):
    a changed source file yields a new key, so the old row/file is simply never
    addressed again (natural staleness -- no invalidation bookkeeping). It is NOT
    unique: two items with byte-identical content share one key/file (free
    cross-item dedup). ``UNIQUE(item_id, tier)`` is the upsert key; a regeneration
    replaces the row and the old file becomes a GC-reclaimable orphan.
    """

    __tablename__ = "thumbnail_manifest"
    __table_args__ = (
        UniqueConstraint("item_id", "tier", name="uq_thumbnail_manifest_item_tier"),
        Index("ix_thumbnail_manifest_cache_key", "cache_key"),
        Index("ix_thumbnail_manifest_item", "item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), nullable=False
    )
    tier: Mapped[int] = mapped_column(SmallInteger)  # 0=grid(320), 1=preview(800)
    cache_key: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    height: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # 'artwork' (sidecar poster/thumb) | 'image' | 'audio_embedded' | 'video'.
    source: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


# --------------------------------------------------------------------------- #
# Phase 6 — Identity, Auth & RBAC (P6-T1 schema). The identity FOUNDATION:     #
# principals (the abstract actor), users (human local/federated accounts),     #
# service_accounts (non-human actors that own api_keys, wired in a later       #
# additive pass), and sessions (Postgres-backed cookie sessions — the O(1)     #
# instant-revocation store that replaces stateless JWT, research §1.3).        #
# Groups + path_grants (the RBAC ACL layer) land in P6-T2; nothing here is     #
# wired into enforcement beyond local login + the session/api-key auth gate.   #
# See docs/tasks/phase-6-identity-auth-rbac-tasks.md (§ Intended DDL).         #
# --------------------------------------------------------------------------- #


class Principal(Base):
    """The abstract authenticated actor — a human ``user`` or a non-human
    ``service_account``. ``global_role`` is the coarse first RBAC layer (the
    *ceiling*; path grants can only narrow within it — see ``filearr.rbac``).
    ``disabled_at`` is a soft-disable that preserves audit history (a disabled
    principal can never authenticate but its past actions stay attributable)."""

    __tablename__ = "principals"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('user','service_account')", name="principals_kind_valid"
        ),
        CheckConstraint(
            "global_role IN ('admin','user','viewer')",
            name="principals_global_role_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    kind: Mapped[str] = mapped_column(Text)  # 'user' | 'service_account'
    global_role: Mapped[str] = mapped_column(Text, server_default=text("'viewer'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    disabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class User(Base):
    """A human account. ``password_hash`` is an argon2id encoded string (NEVER
    the API-key sha256-at-rest pattern — a human password is low-entropy and
    REQUIRES a slow, memory-hard KDF, research §1.1). It is NULL for
    LDAP/SAML/OIDC-only accounts whose credentials the IdP verifies.

    Case-insensitive username uniqueness is enforced by a functional unique
    index on ``lower(username)`` (``uq_users_username_lower``) rather than the
    ``citext`` extension, so no contrib extension is required on the target
    Postgres. The app normalizes usernames to lowercase on create and lookup."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "auth_provider IN ('local','ldap','saml','oidc')",
            name="users_auth_provider_valid",
        ),
        Index(
            "uq_users_username_lower",
            text("lower(username)"),
            unique=True,
        ),
        # P6-T5 federated identity linking. An OIDC subject (``sub``) is unique
        # only WITHIN its issuer, so identity = (auth_provider, external_issuer,
        # external_subject). Partial-unique (only where a subject is present) so
        # the many local rows — all NULL subject — never collide.
        Index(
            "uq_users_external_identity",
            "auth_provider",
            "external_issuer",
            "external_subject",
            unique=True,
            postgresql_where=text("external_subject IS NOT NULL"),
        ),
    )

    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    )
    username: Mapped[str] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_provider: Mapped[str] = mapped_column(Text, server_default=text("'local'"))
    # P6-T5: federated subject + its issuing IdP. NULL for local accounts. The
    # (auth_provider, external_issuer, external_subject) triple is the stable
    # identity an SSO login re-resolves to (never the mutable username/email).
    external_issuer: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ServiceAccount(Base):
    """A first-class non-human principal (research §1.3, Grafana's model). Owns
    ``api_keys`` — the ApiKey backfill that re-homes today's bare keys under a
    service account is a later additive migration (P6-T1 tasks doc § ApiKey
    backfill note); this table ships now so that migration has a target."""

    __tablename__ = "service_accounts"

    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text)


class Session(Base):
    """A Postgres-backed interactive session (research §1.3). ``session_hash`` is
    the sha256 of the opaque cookie value — the raw token is shown to the browser
    once and NEVER persisted (mirrors the API-key pattern). Deleting a row
    invalidates the cookie on the very next request (the O(1) instant-revocation
    property that motivates Postgres sessions over stateless JWT).

    Lifecycle (Grafana defaults, ``FILEARR_SESSION_*``-tunable): 7d inactivity
    (``last_seen_at``), 30d absolute (``expires_absolute``, fixed at creation),
    10min token rotation (``rotated_at``)."""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_principal", "principal_id"),
        Index("ix_sessions_expiry", "last_seen_at", "expires_absolute"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    session_hash: Mapped[str] = mapped_column(Text, unique=True)  # sha256 hex, never raw
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    expires_absolute: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rotated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)



class OidcLoginState(Base):
    """P6-T5: server-side single-use OIDC authorization-request state. Holds the
    CSRF ``state`` (PK), the ``nonce`` bound into the ID token, and the PKCE
    ``code_verifier`` — NONE of which may live in a client cookie (SameSite=Lax
    would not even carry a cookie on the cross-site callback). A row is created at
    ``/auth/oidc/login`` and DELETED the instant it is consumed at the callback
    (single-use); a presented state that is missing or older than
    ``FILEARR_OIDC_LOGIN_STATE_TTL_MINUTES`` is rejected (replay / stale). Expired
    rows are also swept opportunistically on each new login so the table stays
    bounded without a dedicated job."""

    __tablename__ = "oidc_login_states"
    __table_args__ = (Index("ix_oidc_login_states_created", "created_at"),)

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    nonce: Mapped[str] = mapped_column(Text)
    code_verifier: Mapped[str] = mapped_column(Text)
    return_to: Mapped[str] = mapped_column(Text, server_default=text("'/'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


# --------------------------------------------------------------------------- #
# P6-T2 — RBAC groups + path-scoped ACL grants                                #
# --------------------------------------------------------------------------- #
class PrincipalGroup(Base):
    """A permission group — the RBAC grouping unit shared by human users and
    (per phase-5) machine groups. A ``path_grants`` row targets either a group
    (all its members) or a single principal directly (``subject_kind``). ``name``
    is unique. ``source``/``external_ref`` are forward-compat for the federation
    providers (P6-T5/6/7 map an IdP group claim / LDAP DN onto a local group);
    for a local group ``source='local'`` and ``external_ref`` is NULL."""

    __tablename__ = "principal_groups"
    __table_args__ = (
        CheckConstraint(
            "source IN ('local','ldap','saml','oidc')", name="principal_groups_source_valid"
        ),
        UniqueConstraint("name", name="uq_principal_groups_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, server_default=text("'local'"))
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class PrincipalGroupMember(Base):
    """Membership edge (principal ∈ group). Composite PK makes each membership
    unique; both FKs cascade so deleting a principal or a group prunes the edge
    (never orphans a grant's audience)."""

    __tablename__ = "principal_group_members"
    __table_args__ = (
        Index("ix_principal_group_members_group", "group_id"),
    )

    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principals.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("principal_groups.id", ondelete="CASCADE"), primary_key=True
    )


class PathGrant(Base):
    """One path-scoped ACL grant (brief §2.3, P6-T2). ``subject_kind`` +
    ``subject_id`` name the audience — a single principal or a whole group (a
    polymorphic reference, so no single DB FK; existence is validated at the API
    layer). ``scope`` is the ltree-encoded ``lib_<uuid.hex>.<rel_path>`` prefix
    (from ``rbac.path_to_ltree``) the grant covers; ``action`` is ONE member of
    ``rbac.ACTIONS``; ``effect`` is ``allow`` or ``deny`` (explicit-deny-wins at
    equal specificity — see ``rbac.evaluate``). ``library_id`` pins the grant to
    the library whose label heads ``scope`` (kept explicit for CRUD + rebuild).

    The pure ``rbac.PathGrant`` dataclass is the in-memory projection of a row;
    the evaluation engine never touches this ORM class directly."""

    __tablename__ = "path_grants"
    __table_args__ = (
        CheckConstraint(
            "subject_kind IN ('principal','group')", name="path_grants_subject_kind_valid"
        ),
        CheckConstraint(
            "effect IN ('allow','deny')", name="path_grants_effect_valid"
        ),
        CheckConstraint(
            "action IN ('search_metadata','search_content','download','upload',"
            "'modify','delete','edit_metadata','manage_alerts')",
            name="path_grants_action_valid",
        ),
        Index("ix_path_grants_subject", "subject_kind", "subject_id"),
        Index("ix_path_grants_library", "library_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    subject_kind: Mapped[str] = mapped_column(Text)  # 'principal' | 'group'
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE")
    )
    scope: Mapped[str] = mapped_column(LtreeCompat())  # ltree-encoded prefix (see migration)
    action: Mapped[str] = mapped_column(Text)
    effect: Mapped[str] = mapped_column(Text, server_default=text("'allow'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("principals.id", ondelete="SET NULL"), nullable=True
    )


# --------------------------------------------------------------------------- #
# P6-T8 — brute-force rate-limit state (Postgres fixed-window + lock)          #
# --------------------------------------------------------------------------- #
class AuthRateLimit(Base):
    """One rate-limit bucket for a credential-check path (P6-T8). ``bucket_kind``
    is ``username`` (the submitted username string, catching a distributed brute
    force) or ``ip`` (the source address); ``bucket_key`` is the value. The pair
    is the composite PK, so the two independent buckets never collide.

    Fixed-window semantics: ``attempts`` accumulates within a
    ``FILEARR_AUTH_RATELIMIT_WINDOW_SECONDS`` window opened at ``window_start``;
    when it reaches the max, ``locked_until`` is set. Mutations commit in their
    OWN transaction so a failed login's endpoint-level rollback never discards
    the counter. Rows are self-healing (a stale window resets on the next
    failure) and swept lazily; no dedicated purge job is required."""

    __tablename__ = "auth_rate_limits"
    __table_args__ = (
        CheckConstraint(
            "bucket_kind IN ('username','ip')", name="auth_rate_limits_kind_valid"
        ),
        Index("ix_auth_rate_limits_locked", "locked_until"),
    )

    bucket_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    bucket_key: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


# --------------------------------------------------------------------------- #
# P6-T9 — security audit log                                                   #
# --------------------------------------------------------------------------- #
class SecurityEvent(Base):
    """One auditable security event (P6-T9). Append-only. ``principal_id`` is a
    nullable FK that SET NULLs on principal delete so history outlives the
    account (an unknown/unauth actor — a failed login for a nonexistent user —
    has a NULL principal and only ``username_attempted``). ``ip`` and a truncated
    ``user_agent`` capture provenance; ``details`` is a small JSONB bag that is
    secret-scrubbed before write (never a password/token/key). Writes happen in
    their OWN transaction and any failure is logged-and-swallowed — auditing must
    never break the authentication path it observes."""

    __tablename__ = "security_events"
    __table_args__ = (
        Index("ix_security_events_ts_id", text("ts DESC"), text("id DESC")),
        Index("ix_security_events_principal", "principal_id"),
        Index("ix_security_events_type", "event_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    event_type: Mapped[str] = mapped_column(Text)
    principal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("principals.id", ondelete="SET NULL"),
        nullable=True,
    )
    username_attempted: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


# --------------------------------------------------------------------------- #
# Phase 5 — Distributed agents (P5-T1). The central-side trust anchor for the  #
# fleet: an ``agents`` registry (server-assigned identity, R3) and single-use, #
# short-TTL ``enrollment_tokens`` (hashed at rest, presented-once, R3). The     #
# agent-local outbox/index and the ``agent_replication_log`` idempotency ledger #
# are LATER tasks (P5-T3/T4); only these two tables land in P5-T1. See          #
# docs/tasks/phase-5-distributed-agents-tasks.md (§ Intended central-side DDL)  #
# and docs/research/phase-5-distributed-agents.md §7.1/§7.2.                    #
#                                                                              #
# DDL deviation (documented, forced by R3): the brief's DDL types              #
# ``agents.cert_fingerprint`` as ``NOT NULL UNIQUE``. R3 mandates register-     #
# BEFORE-cert (server assigns the id, THEN the agent CSRs against the CA), so   #
# at registration time no cert — and therefore no fingerprint — exists yet.     #
# The column is therefore NULLABLE with a PARTIAL unique index (unique only     #
# among bound fingerprints); a freshly-registered agent is "pending" until the  #
# fingerprint is bound (P5-T2's agent↔step-ca flow completes it).               #
# --------------------------------------------------------------------------- #


class Agent(Base):
    """A registered fleet agent (P5-T1, research §7.2). ``id`` is server-assigned
    at ``/agents/register`` (R3) and is authoritative — it is what the agent
    embeds in its cert CN/SAN, never a client-chosen value. ``cert_fingerprint``
    is bound AFTER the CA signs (nullable while pending; see the module note).
    ``revoked_at`` is the application-layer kill switch (research §1.4): a
    non-null value denylists the agent on every replication/config request
    regardless of whether its short-lived cert is still cryptographically valid.
    ``enroll_secret_hash`` is a one-time, sha256-hashed nonce returned once from
    register and required to bind the fingerprint — it closes the window where a
    third party who guessed a pending agent's UUID could hijack its identity by
    binding their own cert (hardened further by mTLS in P5-T2)."""

    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            "platform IN ('windows','macos','linux')", name="agent_platform_valid"
        ),
        Index("ix_agents_rollout_group", "rollout_group"),
        Index(
            "ix_agents_cert_fingerprint",
            "cert_fingerprint",
            unique=True,
            postgresql_where=text("cert_fingerprint IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    name: Mapped[str] = mapped_column(Text)
    hostname: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)  # windows|macos|linux
    # R5: text column now; migrates to phase-6 machine groups (alias/supersede,
    # never two parallel grouping authorities).
    rollout_group: Mapped[str] = mapped_column(Text, server_default=text("'default'"))
    cert_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    enroll_secret_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_contiguous_seq_no: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0")
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # P5-T5 purge-safety watermark (§4.5): the instant the agent's last full-
    # manifest reconciliation completed (start-match OR finish). Gates permanent
    # recycle-bin purge of THIS agent's trashed items alongside the retention
    # window (R2): worker.purge_recycle_bin skips a trashed agent item whose
    # deletion the last sweep has not yet observed (last_reconcile_at IS NULL OR
    # < the item's deleted_at). NULL = never reconciled (blocks purge while the
    # agent is live). A revoked/deleted agent never blocks purge.
    last_reconcile_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agent_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FK-adjacent to phase-2 policy_versions (shape coordinated, not yet a FK).
    policy_version_applied: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class EnrollmentToken(Base):
    """A single-use, short-TTL enrollment token (P5-T1, research §7.1, R3). The
    raw token is shown to the operator ONCE and NEVER persisted — only its
    sha256 (``token_hash``, the PK) is stored, mirroring the API-key / session
    pattern. Single-use is enforced by ``consumed_at``/``consumed_by`` (set
    atomically at register); TTL by ``expires_at`` (minutes-to-hours, research
    §7.1 — the one human-copy-paste weak link, so kept short)."""

    __tablename__ = "enrollment_tokens"
    __table_args__ = (Index("ix_enrollment_tokens_expires", "expires_at"),)

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)  # sha256 hex
    rollout_group: Mapped[str] = mapped_column(Text, server_default=text("'default'"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class PolicyVersion(Base):
    """Append-only, audit-trailed agent policy version (P5-T6, research §6.3).

    The single source of policy an agent polls at ``GET /agents/{id}/policy``.
    One row PER (scope, version); a policy edit NEVER mutates an existing row —
    it inserts a new one at ``version = prior scope max + 1``, so the full history
    is preserved (the console's "which agent is on which version" view, §6.3).

    Scope precedence (most-specific-wins, NO merging in v1 — the winning row IS
    the effective policy): ``agent`` > ``group`` > ``global``. ``scope_id`` is
    NULL for ``global``, the ``rollout_group`` name for ``group``, and the agent
    UUID-as-text for ``agent`` (text, not a UUID FK: a group name is never a
    UUID, and keeping one column type across the three scopes is simpler than a
    polymorphic id). Merging across scopes is a documented future option.

    ``policy`` is stored VERBATIM (unknown forward-compat keys preserved so an
    older central never strips a newer agent's keys); the API validates the known
    v1 keys but passes unknown ones through untouched.

    A future **phase-2 Stage B** policy-versioning feature REUSES this exact table
    (it was designed here first because P5-T6 shipped before P2 Stage B); do not
    invent a second policy-versioning scheme.

    NOTE on the unique constraint: because ``scope_id`` is NULL for ``global`` and
    Postgres treats NULLs as distinct in a UNIQUE constraint, uniqueness for the
    global scope is enforced by the application's ``max(version)+1`` append (the
    single-operator write path), not by the constraint alone — the constraint is a
    backstop for the group/agent scopes where ``scope_id`` is non-null."""

    __tablename__ = "policy_versions"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('global','group','agent')",
            name="policy_versions_scope_type_valid",
        ),
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "version",
            name="uq_policy_versions_scope_version",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "ix_policy_versions_scope_version",
            "scope_type",
            "scope_id",
            text("version DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    scope_type: Mapped[str] = mapped_column(Text)  # global|group|agent
    scope_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer)
    policy: Mapped[dict] = mapped_column(JSONB)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AgentReplicationLog(Base):
    """The per-agent replication idempotency ledger (P5-T4, research §7.2).

    One row per applied outbox entry, keyed by the composite ``(agent_id,
    seq_no)`` primary key — the same ``seq_no`` the agent stamps in its durable
    outbox (``AgentEvent.seq_no``). :func:`filearr.agentsync.apply_batch` inserts
    a row for EVERY entry in an accepted batch (``ON CONFLICT DO NOTHING`` so a
    pathological replay can never double-apply) in the SAME transaction that
    writes the items and advances ``agents.last_contiguous_seq_no``. The endpoint
    gates a batch through ``check_batch`` against that watermark first, so the PK
    is a backstop, not the primary guard.

    ``item_id`` is the central item the entry resolved to (nullable: a tombstone
    against an already-purged row — the R2 counted no-op — has no item, and a
    collapsed-away entry likewise). ``ON DELETE SET NULL`` so a later recycle-bin
    purge of the item keeps the ledger's audit/reconciliation history intact.
    ``op`` records the entry's ``event_type`` (created/modified/deleted/moved)."""

    __tablename__ = "agent_replication_log"
    __table_args__ = (
        Index("ix_agent_replication_log_item", "item_id"),
    )

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq_no: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="SET NULL"),
        nullable=True,
    )
    op: Mapped[str] = mapped_column(Text)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AgentReconcileSession(Base):
    """One in-flight full-manifest reconciliation sweep for an agent (P5-T5,
    research §4.4). Created when :func:`filearr.agentsync.reconcile_start` finds
    the agent's whole-library digest disagreeing with central's projection; the
    agent then pages its manifest into :class:`AgentReconcileStaging` and calls
    finish, which verifies + anti-joins + drops the session.

    ``unique(agent_id)`` enforces **exactly one live session per agent**: a new
    ``start`` supersedes any prior unfinished session (deleted first). A session
    whose ``started_at`` ages past ``FILEARR_AGENT_RECONCILE_SESSION_TTL_SECONDS``
    is treated as expired (404) and swept opportunistically at the next start."""

    __tablename__ = "agent_reconcile_sessions"
    __table_args__ = (
        Index("uq_agent_reconcile_sessions_agent", "agent_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE")
    )
    library_ref: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    staged_rows: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class AgentReconcileStaging(Base):
    """One staged manifest row for an in-flight reconcile sweep (P5-T5). The
    agent pages its FULL manifest here; finish recomputes the digest over these
    rows (must equal the agent's asserted digest) and anti-joins them against
    central ``items``. ``mtime_us`` is INTEGER microseconds — the cross-language
    digest quantum (:func:`filearr.agentsync.mtime_to_us`, ruling 2) — stored
    directly so the finish recompute never re-quantizes. PK ``(session_id,
    rel_path)`` makes a re-sent page idempotent (upsert). CASCADE on the session
    FK so dropping/expiring a session reclaims its staging in one delete."""

    __tablename__ = "agent_reconcile_staging"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_reconcile_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    rel_path: Mapped[str] = mapped_column(Text, primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_us: Mapped[int] = mapped_column(BigInteger)
    quick_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------- #
# Phase 10 — Agent command primitive (P10-T1). The on-demand instruction        #
# channel distinct from Phase-5's policy/replication channels (research §3.1,   #
# osquery ``distributed_interval`` precedent). Central enqueues one command per  #
# row (``stat_check`` / ``rehash_check`` / ``stage_upload``); an agent long-     #
# polls, picks it up (``pending`` → ``picked_up``), and reports a ``result``     #
# (``done`` / ``failed``). A TTL sweep flips unpicked/leased-past-deadline rows  #
# to ``expired`` and re-queues unacked deliveries (at-least-once). See           #
# docs/tasks/phase-10-agent-transfer-tasks.md (P10-T1) + docs/research/          #
# phase-10-agent-file-transfer.md §3.1.                                         #
#                                                                              #
# DDL deviations from the tasks-doc DDL (documented, additive, forced by the    #
# central-side admin/API surface this task builds — see the migration + report):#
#   * ``status`` CHECK adds ``'cancelled'`` — the admin cancel action (a         #
#     pre-terminal command an operator abandons) is materially distinct from     #
#     ``expired`` (TTL lapsed, "agent never came back") and ``failed`` (the      #
#     agent tried and failed); collapsing it into either loses information the   #
#     UI chip + security_events audit need.                                     #
#   * ``attempts`` — bounds redelivery (a persistently-crashing agent stops      #
#     being re-queued after ``FILEARR_AGENT_COMMAND_MAX_ATTEMPTS``, independent  #
#     of the wall-clock ``expires_at`` bound).                                   #
#   * ``updated_at`` — last-transition timestamp for the UI + keyset stability.  #
# ``requested_by`` gets ``ON DELETE SET NULL`` (not a bare FK) so the audit      #
# actor link outlives a deleted principal, mirroring ``security_events``.        #
# --------------------------------------------------------------------------- #


class AgentCommand(Base):
    """One on-demand instruction for a fleet agent (P10-T1, research §3.1).

    Rides the same authenticated poll infrastructure Phase-5 built, but is a
    SEPARATE channel from policy/replication (osquery ``distributed_interval``
    precedent): ``kind`` covers existence checks (``stat_check`` /
    ``rehash_check``) and the retrieve trigger (``stage_upload``) on one
    primitive. Lifecycle (``filearr.agentsync.command_state_machine``):
    ``pending`` → ``picked_up`` (agent poll delivers it) → ``done`` / ``failed``
    (agent reports a ``result``); a TTL sweep flips a stale ``pending`` /
    lease-lapsed ``picked_up`` row to ``expired`` (kept, not deleted, so the UI
    can say "the agent never came back"), re-queues an unacked delivery to
    ``pending`` (bounded by ``attempts``), and an admin may ``cancel`` any
    pre-terminal row. ``done`` / ``failed`` / ``expired`` / ``cancelled`` are
    terminal and immutable. Idempotent by construction: re-picking-up /
    re-completing a terminal row is a no-op (same posture as replication's
    ``(agent_id, seq_no)`` upsert)."""

    __tablename__ = "agent_commands"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('stat_check','rehash_check','stage_upload')",
            name="agent_commands_kind_valid",
        ),
        CheckConstraint(
            "status IN ('pending','picked_up','done','failed','expired','cancelled')",
            name="agent_commands_status_valid",
        ),
        # Doc DDL index: fast per-agent FIFO drain of undelivered work.
        Index(
            "ix_agent_commands_pending",
            "agent_id",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        # Keyset list ordering (id is uuidv7 → time-ordered), admin console.
        Index("ix_agent_commands_id_desc", text("id DESC")),
        # Sweep target: only ever scans non-terminal rows (expiry + redelivery).
        Index(
            "ix_agent_commands_sweep",
            "expires_at",
            postgresql_where=text("status IN ('pending','picked_up')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)  # stat_check | rehash_check | stage_upload
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE")
    )
    payload: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    picked_up_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("principals.id", ondelete="SET NULL"),
        nullable=True,
    )


class AgentRelease(Base):
    """One uploaded, SIGNED agent release + its rollout stage (P5-T7, research
    §5.1/§6.3).

    ``manifest`` is the minisign-style manifest EXACTLY as signed on the
    operator's signing machine, INCLUDING its Ed25519 ``signature`` field.
    Central stores and serves it but CANNOT re-sign it (the private key never
    reaches central) — the agent verifies the signature against its build-time
    pinned public key, so a compromised central cannot push a wrongly-signed
    binary (research §8 threat model: central is untrusted for update integrity).

    ``stage`` gates the staged rollout (R5): a fresh upload is ``'canary'`` (seen
    only by agents whose ``rollout_group`` is the canary group); the operator
    ``promote`` action flips it to ``'general'`` (seen by the whole fleet) — the
    §6.3 operator-confirmation gate, taken after checking canary health via the
    per-agent confirmed-version rollup. Artifact BINARIES live under
    ``FILEARR_AGENT_RELEASES_DIR`` (not this table); only manifest metadata is in
    Postgres."""

    __tablename__ = "agent_releases"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('canary','general')", name="agent_releases_stage_valid"
        ),
        UniqueConstraint("version", name="uq_agent_releases_version"),
        Index(
            "ix_agent_releases_stage_created",
            "stage",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    version: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text, server_default=text("'canary'"))
    manifest: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    promoted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StagingTransfer(Base):
    """One in-flight agent->central retrieve upload (P10-T4, research §2/§6).

    The durable resume anchor for the hand-rolled offset-``PATCH`` staging
    protocol (docs/research/phase-10-t4-transport-spike.md): an agent that picks
    up a ``stage_upload`` command attaches (creates-or-returns) exactly one row
    per ``command_id`` (``uq_staging_transfers_command`` — idempotent so a
    restarted agent, or an at-least-once command redelivery, re-attaches the SAME
    row), then streams the file body in chunks. Central advances
    ``bytes_transferred`` ONLY after each chunk is durably written, so the resume
    point is always exactly ``bytes_transferred`` — central is the single source
    of truth for where a restarted upload continues.

    ``state`` walks the ``transfers.TransferState`` lifecycle
    (``pending`` -> ``uploading`` -> ``staged`` -> ``downloaded``; ``expired`` /
    ``failed`` terminals) and is advanced ONLY through
    ``transfers.transfer_state_machine``. On the final byte the row goes
    ``staged`` with ``verified=False``: streaming hash verification is P10-T5, a
    clear seam here (no download is served on an unverified row). ``item_id`` /
    ``agent_id`` are derived from the command server-side (never agent-supplied),
    so an agent cannot stage a transfer for an item/agent it does not own.
    ``staged_path`` is ``<transfer_uuid>.bin`` (``transfers.staging_path_for``,
    traversal-proof by construction) under ``FILEARR_STAGING_DIR`` — writable
    central disk, NOT a media mount (R5; invariant 6 untouched). TTL cleanup is
    P10-T8 (``expires_at`` / ``last_range_request_at`` are its anchors)."""

    __tablename__ = "staging_transfers"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending','uploading','staged','downloaded','expired','failed')",
            name="staging_transfers_state_valid",
        ),
        CheckConstraint(
            "bytes_transferred >= 0", name="staging_transfers_bytes_nonneg"
        ),
        CheckConstraint(
            "total_bytes IS NULL OR total_bytes >= 0",
            name="staging_transfers_total_nonneg",
        ),
        # Idempotent attach: exactly one transfer per stage_upload command.
        UniqueConstraint("command_id", name="uq_staging_transfers_command"),
        # P10-T6 race fix: at most ONE ACTIVE transfer per item. The initiate
        # duplicate-active guard is check-then-insert; this partial unique index
        # is the backstop that makes a concurrent double-initiate (or a stray
        # second stage_upload command) fail closed at the DB, converted to the
        # same 409-with-existing-id contract in the API. A terminal transfer
        # (downloaded/expired/failed) leaves the index, so the item can be
        # retrieved again.
        Index(
            "uq_staging_transfers_active_item",
            "item_id",
            unique=True,
            postgresql_where=text("state IN ('pending','uploading','staged')"),
        ),
        # P10-T8 sweep target: only ever scans non-terminal rows.
        Index(
            "ix_staging_transfers_expires",
            "expires_at",
            postgresql_where=text("state IN ('pending','uploading','staged')"),
        ),
        Index("ix_staging_transfers_item", "item_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("items.id", ondelete="CASCADE")
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE")
    )
    command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_commands.id", ondelete="CASCADE")
    )
    state: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    bytes_transferred: Mapped[int] = mapped_column(
        BigInteger, server_default=text("0")
    )
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    staged_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_range_request_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    # P10-T8 last-activity watermark: bumped on every UPDATE (a PATCH append that
    # advances the offset, the staged/verified transition, a download watermark).
    # The staging TTL sweep uses it to tell an actively-progressing partial upload
    # from an abandoned one (no progress for ``staging_abandoned_upload_seconds``).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        onupdate=func.now(),
    )


# --------------------------------------------------------------------------- #
# Phase 10 — Central agent share-mapping fallback (P10-T12, user-mandated).     #
# When an agent can't self-report a network ``ShareHint`` (P10-T11 is best-      #
# effort, R1), an operator declares centrally how a path on an agent maps to a   #
# network share, so an agent-hosted item still renders a network-open link. The  #
# per-AGENT equivalent of OPS-T7's deploy-time ``share-map.json`` (which covers   #
# the CENTRAL server's own mounts). Resolution reuses the pure longest-          #
# ``local_prefix``-wins resolver (``transfers.resolve_share_url`` /               #
# ``share_map.resolve_for_agent``, R4); an agent-scoped rule outranks a global    #
# (``agent_id IS NULL`` = any agent) one of equal prefix length. Item wiring is   #
# a P10-T2 follow-up (agent-owned items via ``items.source_agent_id``).           #
#                                                                              #
# DDL deviations from the tasks-doc DDL (documented in the migration + report):   #
#   * ``unc`` — optional Windows counterpart of ``share_prefix`` (UI-T15); when   #
#     absent it is DERIVED from ``share_prefix`` at read time (SMB only).         #
#   * ``storage_type`` / ``host`` — informational (mirrors ``share_map.py``).     #
#   * ``updated_at`` — last-edit timestamp (edits are supported).                 #
#   * UNIQUE NULLS NOT DISTINCT (agent_id, library_id, local_prefix) backstops    #
#     the app-level 409 dup-prefix check.                                         #
# --------------------------------------------------------------------------- #


class AgentShareMap(Base):
    """One admin-defined central share mapping for agent-hosted files (P10-T12).

    Generalises ``library.share_prefix``: an operator maps a ``local_prefix`` (a
    path as the agent's OS sees the file) to a ``share_prefix`` (the network
    location that reaches it — ``\\host\\share``, ``smb://host/share``,
    ``/Volumes/...``), optionally scoped to one ``agent_id`` (NULL = any agent)
    and/or ``library_id``. Resolution is longest-``local_prefix``-wins
    (:func:`filearr.share_map.resolve_for_agent`), the same discipline as
    ``resolve_scan_path`` / the frontend ``pathlinks``. An agent-scoped rule
    outranks a global one of equal prefix length. ``unc`` is the optional Windows
    counterpart (derived from ``share_prefix`` when absent, SMB only, UI-T15)."""

    __tablename__ = "agent_share_maps"
    __table_args__ = (
        Index("ix_agent_share_maps_agent", "agent_id"),
        Index(
            "uq_agent_share_maps_scope_prefix",
            "agent_id",
            "library_id",
            "local_prefix",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "agents.id", ondelete="CASCADE",
            name="fk_agent_share_maps_agent_id_agents",
        ),
        nullable=True,
    )
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "libraries.id", ondelete="CASCADE",
            name="fk_agent_share_maps_library_id_libraries",
        ),
        nullable=True,
    )
    local_prefix: Mapped[str] = mapped_column(Text)
    share_prefix: Mapped[str] = mapped_column(Text)
    unc: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    host: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
