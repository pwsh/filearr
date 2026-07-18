# Research Brief — Roadmap §7: Data Model Extensions

### Typed metadata profiles, custom fields, display templates, provenance

Companion to `docs/future-roadmap.md` §7 (scope). References but does not
duplicate: §3 (RBAC/`edit_metadata` action — this brief assumes it exists and
plugs into it), §1 (distributed agents — provenance's `source_agent` column
is forward-compatible with that work, not built for it yet). Researched
2026-07-07 against the live stack: FastAPI 0.139 / Python 3.13 / SQLAlchemy 2 +
psycopg3 / Postgres 18.4 / Procrastinate 3.9 / meilisearch-python-sdk 7.x /
Meilisearch v1.48.3 / Svelte 5 runes + Vite 8 + Tailwind v4. Constraint
ordering for every decision below: **security > integrity > reliability >
speed > compatibility > scalability**. AGPL-3.0-or-later — every new
dependency is license-checked explicitly. **JSONB-first, no premature EAV.**

Current baseline (read from the repo, 2026-07-07):
- `backend/filearr/models.py`: `Item.metadata_` (JSONB, extracted, column name
  `metadata`) and `Item.user_metadata` (JSONB, API edits) are separate
  columns, GIN-indexed (`ix_items_metadata` on `metadata` only — `user_metadata`
  has no GIN index today). `effective_metadata` property overlays
  `user_metadata` on `metadata_`. `ItemVersion` is a flat audit table:
  `(id, item_id, changed_at, actor, patch JSONB)` — one row per PATCH/batch
  call, storing the *diff* the caller sent (title/year/tags/user_metadata/
  external_ids), not a full before/after snapshot, and **only covers
  `user_metadata` edits** (extractors never write `ItemVersion`).
- `backend/filearr/tasks/extract.py` + `ffprobe.py` + `documents.py`: per-type
  extractors write flat key/value dicts into `metadata_` via
  `item.metadata_ = {**item.metadata_, **meta}` (shallow merge, last-write-wins
  per top-level key). No schema registry, no validation — whatever the parser
  emits lands directly in JSONB. Known key vocabularies per type today: audio
  (`title/artist/album/genre/year/duration/bitrate/samplerate/channels`),
  image (`width/height/format/mode/camera/taken_at`), video (guessit fields +
  ffprobe's `container/duration/bitrate/video_codec/width/height/resolution/
  frame_rate/hdr/hdr_format/color_primaries/color_transfer/audio_codec/
  audio_tracks/subtitle_tracks`), audiobook (audio fields + chapters),
  model3d (trimesh geometry facts), document/spreadsheet (pypdf/python-docx/
  openpyxl property dicts). Every extractor also has an escape hatch:
  `_extract_error` (string) on failure — this key must be excluded from any
  future profile's "known keys" validation (it is a sentinel, not a data field).
- `backend/filearr/api/items.py`: `PATCH /items/{id}` and `POST /items/batch`
  accept `ItemPatch` (title/year/tags/user_metadata/external_ids). No field-
  level validation beyond Pydantic's type check on `dict[str, Any]` — any key,
  any value shape, is accepted into `user_metadata` today. No admin-scope
  profile/field-definition endpoints exist.
- `backend/filearr/search.py`: `build_doc()` flattens `metadata_` +
  `user_metadata` into a handful of hardcoded top-level Meili fields
  (`artist/album/author/codec/resolution/genre`) — there is no generic
  "project every metadata key as a facet" mechanism, and no per-field
  filterable/searchable/sortable classification driven by data.
- `docs/research/phase-3-search-findability.md` (§5 scope, sibling brief)
  already stakes out: `libraries.expose_gps` boolean (privacy-default-off
  pattern to imitate for any future sensitive field class), `saved_searches`
  table (Postgres-only, ACL-aware, not a Meili concept), a `recency_bucket`
  Meili-only computed field pattern (disposable projection field with no
  Postgres column), and an explicit warning that embeddings/vectors must
  **never** be persisted in Postgres (Meili-only, invariant 1) while a
  model-identifier/version *is* persisted in a settings table so
  `rebuild_index` can detect drift. **This brief's schema must compose with
  all four of those** — see §8 below for the concrete integration points.

---

## 1. Metadata-profile prior art

**Paperless-ngx custom fields** (most directly comparable system: single-owner
documents, per-field typed values, faceting + filtering UI).
[docs.paperless-ngx.com](https://docs.paperless-ngx.com/api/),
[GitHub #7361](https://github.com/paperless-ngx/paperless-ngx/issues/7361),
[GitHub #10514](https://github.com/paperless-ngx/paperless-ngx/discussions/10514)
- **Data model:** a central `CustomField` definition table (id, name, **data
  type** — string/int/float/monetary/boolean/date/url/select/document-link)
  decoupled from a per-document `CustomFieldInstance`-style value row/JSON
  entry. This is the "central definition, applied per-document" shape the
  Filearr scope explicitly asks for ("central definition, per-location
  applicability").
- **Validation is type-tagged at the field-definition level, enforced at
  write time** — e.g., "Monetary" must be an ISO 4217 code + exactly two
  decimals. Field data type **cannot be changed after creation** (a hard
  compatibility rule to avoid silently reinterpreting existing values under a
  new type — directly informs Filearr's own profile-versioning decision,
  §7 below).
- **What broke at scale / documented pain points:** a filed bug
  (#7361) shows *inconsistent* custom-field value validation — different
  code paths (API vs UI vs bulk-edit) applied different strictness, letting
  invalid values slip in through one path while another rejected them. The
  lesson for Filearr: **validation must live in one shared function/layer
  called by every write path** (PATCH, batch PATCH, and any future
  profile-driven extractor merge), not duplicated per endpoint.
  A separate open discussion (#10514) requests conditional
  display/validation of custom fields *based on other document metadata*
  (e.g., field X only valid/required when `document_type == invoice`) —
  Paperless does not have this yet; it is a reasonable v3+ idea for Filearr
  but explicitly **out of scope** for this phase (adds a rules-engine
  dimension neither the scope nor the current schema asks for).
- Same name cannot be assigned twice to one document; fields are optional by
  default (no forced schema completeness) — matches Filearr's own
  "JSONB bag, sparse by design" philosophy already stated in `models.py`'s
  docstring.

**Nextcloud metadata** — no authoritative typed-custom-field system was found
comparable to Paperless's; its Files metadata (Recognize/EXIF app-level tags)
is closer to Filearr's *extracted* `metadata` column than to a user-defined
field system, so it's not a strong precedent for the profile/custom-field
question. Not incorporated further.

**DSpace** (digital repository, decades of production metadata-schema
practice). [DSpace 7.x wiki](https://wiki.lyrasis.org/display/DSDOC7x/Metadata+and+Bitstream+Format+Registries),
[NewDublinCore](https://wiki.lyrasis.org/display/DSPACE/NewDublinCore)
- Ships a namespaced **schema registry**: `dc` (Dublin Core, protected),
  `dcterms` (qualified DC), plus an intentionally empty **`local`
  namespace reserved for site-specific custom fields**. The core recommendation
  across DSpace's own docs: **never edit the protected/standard schemas** —
  add custom fields only in the local namespace, because editing shared
  schemas breaks upgrade paths when a new DSpace version needs to
  migrate those same fields.
  - **Direct translation to Filearr:** per-type "metadata profiles" (this
    brief's core proposal) are the `dc`/`dcterms`-equivalent — versioned,
    code-owned, upgraded in lockstep with extractor releases. Custom
    user-defined fields (the scope's second bullet) are DSpace's `local`
    namespace equivalent — freeform, admin-created, never touched by a
    Filearr code upgrade. **This maps cleanly onto Filearr's existing
    `metadata_`/`user_metadata` split**: profiles validate `metadata_` (the
    extractor-owned, versioned, "protected" side) and custom fields live in
    `user_metadata` (the "local namespace" side) — see §7 for the full
    justification, this is the single most load-bearing design decision in
    this brief.

**CKAN** — no CKAN-specific extensible-schema documentation surfaced in this
research pass (search results returned DSpace-adjacent academic papers
instead); not incorporated as a distinct data point. CKAN's general
reputation (from training-era knowledge, flagged as unverified this pass) is
a similar "schema.json describing dataset extras" model, directionally
consistent with the DSpace/Paperless findings above but not independently
re-confirmed here — do not cite CKAN specifics without a follow-up check.

**ExifTool tag groups**
[exifinjector.com/blog/exif-vs-iptc-vs-xmp-2026](https://exifinjector.com/blog/exif-vs-iptc-vs-xmp-2026)
- The load-bearing lesson is **namespace collision, not validation**: the
  same logical field (e.g. GPS) can appear under multiple *groups*
  (`XMP-video:GPS` vs `XMP-exif:GPS`) with exiftool defaulting to one and
  silently dropping/shadowing the other unless the caller asks for grouped
  output. **Directly relevant to Filearr's profile design:** a per-domain
  metadata profile's key names must be **namespaced by the profile that
  owns them** (e.g. `exif.camera_model` vs a hypothetical future
  `iptc.creator`) if two extractors could ever emit a same-named field with
  different semantics — a real risk once EXIF-deep-extraction (roadmap §5
  P2) and video GPS-track extraction both start writing camera/GPS-shaped
  keys. Recommend **flat top-level keys today** (matches current extractor
  behavior, keeps the JSONB bag simple) but reserve the profile's `type_key`
  as an implicit namespace boundary — i.e., **a profile only ever validates
  the keys it declares owning that item's `media_type`**, so a future
  cross-cutting extractor (EXIF video-GPS) either gets folded into the
  existing `video` profile as new fields or introduces its own explicitly
  distinct key prefix (`gps_*`) rather than colliding blind.

**Immich** [deepwiki.com/immich-app/immich/2.6](https://deepwiki.com/immich-app/immich/2.6-data-model-and-storage),
[deepwiki.com/immich-app/immich/3.3](https://deepwiki.com/immich-app/immich/3.3-metadata-extraction)
- Uses an `AssetMetadataKey` **enum** as its extension point for
  non-EXIF/custom asset metadata (ML inference results, app-specific data) —
  a code-defined, closed set of well-known keys rather than a runtime-defined
  custom-field system. This is architecturally the *opposite* end of the
  spectrum from Paperless's admin-creatable fields: Immich's extensibility is
  a developer concern (add an enum member, ship a release), not an
  operator-facing feature. **Confirms Filearr should not copy Immich's
  approach for the *custom-field* half of scope** (the roadmap explicitly
  wants admin-defined fields, which is a Paperless-shaped feature, not an
  Immich-shaped one) — but Immich's enum-of-known-keys pattern is a
  reasonable **internal** implementation detail for how a *profile* enumerates
  its own extractor-owned fields (a Python enum or frozen list per profile,
  not a DB-editable list, since profiles are code/extractor-owned per the
  DSpace-schema-registry analogy above).
- Immich also independently confirms the metadata-group-collision problem
  (same field, multiple EXIF groups) as a live, currently-open bug in
  production — reinforcing the ExifTool finding above rather than adding a
  new one.

**NocoDB / Baserow** — the research pass could not surface authoritative
technical detail on either platform's underlying field storage
(EAV vs JSONB vs native columns) beyond marketing/comparison content; this is
a genuine research gap, flagged rather than guessed at. What is documented
and usable: NocoDB layers a spreadsheet UI over **existing relational
tables** (not its own EAV store) and offers a `JSON` field type as an escape
hatch for arbitrary key/value data — an architecturally different problem
(schema-on-read over pre-existing SQL tables) from Filearr's "central
field definitions applied across heterogeneous file types," so it is a
weak precedent here. Do not lean on this pair further without a dedicated
follow-up.

**What broke at scale, synthesized across all of the above:**
1. Validation applied inconsistently across write paths (Paperless #7361) —
   Filearr must centralize validation in one function reused by
   `PATCH /items/{id}`, `POST /items/batch`, and any profile-driven extractor
   merge path.
2. Editing "protected"/shared schemas breaks upgrades (DSpace) — Filearr's
   `metadata_`-side profiles are code-shipped and versioned; never allow an
   admin API to mutate a *built-in* profile's field list, only to add
   independent custom fields (the `user_metadata`-side, unversioned, freely
   editable set).
3. Group/namespace collisions silently shadow data (ExifTool, Immich) —
   flat keys are fine at today's scale, but document the namespace-boundary
   rule now so a future extractor addition doesn't silently overwrite a
   same-named key from a different source.
4. Rigid one-way type locking (Paperless: type cannot change after creation)
   is a **feature, not a limitation** — adopt it directly (§7 below) to avoid
   the far worse failure mode of silently reinterpreting old values under a
   new, incompatible type.

---

## 2. JSON Schema validation in Python (2026) + Postgres-side options

**Application-side candidates**

| Library | Approach | Perf (2026 benchmarks found) | Draft support | Verdict for Filearr |
|---|---|---|---|---|
| **`jsonschema` (python-jsonschema)** | Pure-Python, walks a JSON Schema document at validate time | Slowest of the three found; Pydantic validates ~3.5x faster than jsonschema in high-throughput benchmarks | Full multi-draft support (Draft 4 through 2020-12), the reference/most-compliant implementation | Use only if profiles must be expressed as **portable, standard JSON Schema documents** editable by non-Python tooling |
| **Pydantic v2 (dynamic models)** | Compiled (Rust `pydantic-core`) validation from Python type annotations / `create_model()` at runtime | ~3.5x faster than `jsonschema`; already a hard FastAPI 0.139 dependency in this stack (`schemas.py` is Pydantic already) | Not JSON-Schema-spec-driven; Pydantic *emits* JSON Schema for docs, doesn't consume arbitrary external JSON Schema documents as its native validation source | **Recommended** — zero new dependency (already in `pyproject.toml` transitively via FastAPI), and profile definitions can be expressed as a small internal DSL (field name → type → constraints) compiled into a `pydantic.create_model()` call per profile, cached per profile version |
| **msgspec** | Struct-based, ahead-of-time-typed, extremely fast (2-5x faster than Pydantic v2; up to 10-20x faster decode than Pydantic v2, 150x vs Pydantic v1) | Fastest found by a wide margin | JSON Schema *generation* supported; not designed around consuming a dynamic/admin-editable schema shape at runtime as its primary mode | Overkill/wrong shape for this feature — msgspec wants **static** Struct definitions known at import time; Filearr's profiles need runtime-defined field lists driven by an admin-editable table, which fights msgspec's design center. Worth revisiting only if raw scan-time validation throughput becomes a measured bottleneck — not indicated by anything in this research. |

**Recommendation: Pydantic v2 dynamic models (`create_model()`), application-
side only — no Postgres-side JSON Schema validation.** Rationale, in
constraint-priority order:
- **Integrity:** Postgres-side (`pg_jsonschema` or hand-written
  `jsonb_path_exists`/`CHECK`) does close the "someone ran a manual INSERT or
  a migration bypassed app validation" gap that a Postgres blog post
  explicitly calls out as application-validation's weak point. This is a
  real integrity argument *for* DB-side validation.
- **But it is outweighed by reliability + compatibility risk for this
  project specifically:** `pg_jsonschema` is a Supabase-authored **Rust
  (pgrx) Postgres extension** — it must be compiled into the Postgres image
  or loaded via an extension registry. Filearr's `docker-compose.yml` runs a
  stock `postgres:18` image; adding a compiled native extension means
  either (a) building a custom Postgres image (a new, permanently-maintained
  build artifact, plus Alembic-adjacent extension-install migration
  ceremony) or (b) depending on a base image that bundles it — neither fits
  a project whose CLAUDE.md explicitly flags Postgres-image-mount gotchas
  already as a source of regressions. **License:** research could not
  fully confirm `pg_jsonschema`'s license text in this pass (Supabase's own
  projects are generally Apache-2.0, and the crate is a thin wrapper around
  the Rust `jsonschema` crate, but explicit LICENSE-file confirmation was
  not retrieved) — treat as **unverified, re-check before ever adopting**.
  Given AGPL-compatibility is a hard constraint here, do not add this
  dependency without that explicit confirmation.
- Profile validation errors need to become **structured HTTP 422 API
  responses** (per-field messages) — Pydantic's `ValidationError.errors()`
  already produces exactly this shape natively, matching the existing
  FastAPI error-handling convention in the codebase, whereas a Postgres
  `CHECK` constraint violation surfaces as a generic `IntegrityError` that
  the API layer would have to parse/re-interpret to produce a useful
  per-field error — strictly worse UX for the same integrity property.
- **Compromise that keeps most of the integrity win cheaply:** a
  **lightweight, hand-written Postgres `CHECK` constraint is still worth
  adding as a defense-in-depth backstop** — not full JSON Schema validation,
  just a structural sanity check (e.g., `jsonb_typeof(user_metadata) = 'object'`,
  reserved-key non-collision against profile-owned names) using Postgres
  18's native `JSON_EXISTS()`/`jsonb_path_exists()` (both stable, no
  extension required, PG12+/PG17+ respectively). This catches the
  "bypassed the app entirely" failure mode for the cheap, generic invariants
  without taking on a native extension dependency for full schema
  validation. Full per-field type/range validation stays applica­tion-side.

---

## 3. Faceting custom fields into Meilisearch

[Meilisearch known limitations](https://www.meilisearch.com/docs/resources/help/known_limitations),
[Filterable Attributes Setting API spec](https://specs.meilisearch.dev/specifications/text/0123-filterable-attributes-setting-api.html),
[Zero-downtime index deployment](https://www.meilisearch.com/blog/zero-downtime-index-deployment),
[Swap Indexes API spec](https://specs.meilisearch.dev/specifications/text/0191-swap-indexes-api.html)

- **Hard limits found:** a maximum of **65,536 attributes per index**
  (functionally unreachable for Filearr's field counts — not a practical
  constraint even with dozens of custom fields per library); individual
  filterable *values* capped at 468 bytes (matters for long free-text custom
  field values used as facets — validate/truncate at the profile-field
  definition level, e.g. disallow `facetable: true` on a `text`-type custom
  field without a max-length constraint); facet-value *responses* capped at
  100 distinct values returned per facet (a UI pagination concern for
  high-cardinality custom fields, not a data-loss risk).
- **Settings-update cost (the concrete question the task asked):**
  `filterableAttributes`/`searchableAttributes`/`sortableAttributes` updates
  are async tasks (202 Accepted, tracked in the task queue) — updating them
  is **not free**: per Meilisearch's own zero-downtime-deployment guidance,
  a settings change on a live, populated index is exactly the scenario the
  **index-swap pattern** (already adopted in roadmap §8) exists to make
  safe. The practical cost model: **a settings update alone does not require
  a full reindex of existing documents' content**, but it does trigger a
  re-computation of the affected internal structures (facet DB, search
  DB) proportional to corpus size, and — critically — **running it against
  the live index risks a query-time window where the index is
  transitioning**. Meilisearch's documented mitigation is exactly build-in-
  the-background, verify, then atomic-swap.
- **Interplay with the index-swap pattern (roadmap §8), concretely for
  custom fields:** every time an admin adds/edits a custom-field definition
  with `facetable: true` (i.e., changes what must be `filterableAttributes`),
  Filearr should **not** call `update_filterable_attributes()` against the
  live index directly. Instead: build a new index generation with the full
  target settings (existing static fields + the now-complete dynamic
  custom-field facet list) from Postgres via `rebuild_index`, verify task
  completion, then `swapIndexes`. This reuses the exact mechanism already
  planned for other settings churn in §8 — **no new operational pattern
  needed, just route custom-field-driven filterable-attribute changes
  through the same rebuild-and-swap path rather than an in-place settings
  call.** This also sidesteps a subtler correctness problem: Meilisearch
  infers a field's *facet type* (string vs number vs boolean) from the data
  already indexed under that attribute name; a rebuild-from-Postgres
  guarantees the facet type is derived consistently from the profile/custom-
  field's declared type rather than from whatever documents happened to be
  indexed first.
- **Dynamic filterable attributes at scale — the real churn risk:** if every
  custom field a user ever creates is *automatically* added to
  `filterableAttributes`, a library with many low-value custom fields
  (typo'd duplicates, one-off experiments) bloats the filterable list and
  each edit forces another rebuild-and-swap cycle. **Recommendation:**
  custom-field definitions get an explicit `facetable: bool` toggle
  (default **false**) — an admin opts a field into faceting deliberately,
  not automatically on creation. This keeps the common case (most custom
  fields are just extra display/lookup data, not filter/facet dimensions)
  cheap and keeps `filterableAttributes` churn proportional to actual
  intent rather than field-creation volume.
- **No confirmed additional per-attribute cost cliff** beyond the 65,536
  count ceiling was found in this pass — the practical governing constraint
  for Filearr is operational (rebuild-and-swap cadence), not a hard
  Meilisearch limit.

---

## 4. Display templates: server-driven UI vs frontend registry

**Prior art surveyed**

- **Home Assistant Lovelace custom cards:**
  [developers.home-assistant.io/docs/frontend/custom-ui/custom-card](https://developers.home-assistant.io/docs/frontend/custom-ui/custom-card/)
  — registration is a **global-array + custom-element convention**
  (`window.customCards.push({type, name, ...})`), each card is a plain custom
  element implementing `setConfig(config)`; framework-agnostic by design
  (explicitly *excludes* React, permits Polymer/Angular/Preact/vanilla). This
  is architecturally a **typed frontend component registry**, not a JSON
  template DSL — the "schema" is just "which custom element tag to
  instantiate," and per-card configuration is a plain object the card
  interprets itself.
- **Grafana panel plugins:**
  [grafana.com/developers/plugin-tools](https://grafana.com/developers/plugin-tools/tutorials/build-a-panel-plugin),
  [DeepWiki plugin system](https://deepwiki.com/grafana/grafana/11-plugin-system)
  — a more formal version of the same idea: TypeScript/React components
  implementing a standard panel interface, declared via a `plugin.json`
  manifest (id/type/version-compat metadata), registered into an in-memory
  plugin registry at startup, selectable per-dashboard-panel from a catalog.
  Also a **typed component registry**, with the manifest only describing
  *identity/compatibility metadata*, never the rendering logic itself (that
  stays in real, compiled component code). Grafana additionally has a
  "Frontend Plugin Extension" system for contributing UI fragments into
  fixed *extension points* (slots) — a decoupled-integration idea more
  relevant to a future plugin-marketplace than to Filearr's per-media-type
  card/detail view problem.
- **Directus displays/interfaces:**
  [directus.com/docs/guides/extensions/app-extensions/displays](https://directus.com/docs/guides/extensions/app-extensions/displays),
  [directus.io/docs/guides/extensions/app-extensions/interfaces](https://directus.io/docs/guides/extensions/app-extensions/interfaces)
  — the most directly analogous prior art to Filearr's stated need (image →
  preview+EXIF grid; audio → waveform+tags; etc.): a **dual-system split**
  between **Interfaces** (edit-time input components — the "how do I let a
  user change this value" concern) and **Displays** (read-time rendering
  components — "how do I show this value nicely" concern), both are real
  Vue components receiving `(value, options)` as props, registered by
  extension entrypoint, selected **per field-type**, not via a JSON template
  language. Directus's own "Display Templates" feature (a separate concept —
  string-interpolation templates like `{{title}} ({{year}})` for compact
  list-row labels) *is* a small textual DSL, but it is scoped narrowly to
  "how do I summarize a row as one line of text," not to full card/detail
  layout — it is not evidence for a general layout-DSL approach.

**Synthesis and recommendation: typed Svelte component registry, not a JSON
template DSL.** Justification:
- **Every mature precedent surveyed (Home Assistant, Grafana, Directus)
  converges on the same shape:** a small manifest/keying mechanism (type
  string → component identity) plus a **real, code-written component** doing
  the actual rendering — none of them invented a general-purpose layout DSL
  for this problem. The recurring reason: card/detail rendering needs
  arbitrary presentation logic (a waveform needs an actual audio-buffer
  visualization, a mesh-stats panel needs 3D-aware formatting, an EXIF grid
  needs conditional field grouping) that a declarative JSON template would
  have to reinvent a scripting layer to express — at which point it stops
  being simpler than just writing a component. Directus's one real DSL
  (display templates) stays deliberately narrow (string interpolation only)
  precisely because it avoids that trap.
- **Svelte 5 makes this cheap and idiomatic today:** in runes mode,
  `<svelte:component>` is deprecated because **components are dynamic by
  default** — `{@const Component = registry[item.media_type]}` followed by
  `<Component {item} />` (or even `<column.template {item} />` directly) is
  now the native pattern, not a special case requiring extra ceremony. This
  removes what used to be Svelte 4's main argument *for* a config-driven
  DSL (dynamic component selection needed explicit `<svelte:component>`
  boilerplate) — the ergonomics gap that used to justify a DSL layer no
  longer exists.
- **Concrete design for Filearr:**
  - A **registry object keyed by `media_type`** (`image`, `audio`, `video`,
    `audiobook`, `model3d`, `document`, `spreadsheet`, `other` — matching
    the existing `MediaType` enum in `models.py` exactly) mapping to a
    Svelte component for the **card** (compact list/grid view) and a
    second registry for the **detail** view. A media type with no
    registered component falls back to a **generic "key facts" component**
    (already implied by scope: "generic → key facts") that lists
    `effective_metadata` as plain rows — this is also the natural fallback
    for any *future* media type added to the enum before its dedicated
    component ships, so the registry is never a hard blocker for extractor
    work landing ahead of frontend polish.
  - Each per-type component receives the full `Item` (or at minimum
    `effective_metadata` + core columns) as props and owns its own internal
    layout — e.g. the audio component internally decides waveform-vs-tags
    arrangement; Filearr does not need a generic "grid of N cells" template
    language to express that.
  - The **"Raw" tab** (always-available, scope-mandated) is trivially a
    single generic component — not part of the per-type registry at all —
    that dumps core columns + `metadata_` + `user_metadata` + provenance
    fields (§5 below) as a flat key/value table. This is the simplest
    possible implementation and satisfies "every stored field visible"
    literally, by construction, with no per-type maintenance burden as new
    metadata keys are added by extractors over time.
  - **Profiles (§7 below) inform the registry but do not replace it:** a
    profile's declared field list can be used to *order/label* fields within
    a generic fallback or within the "key facts" component (e.g., render
    fields in the profile's declared order with its declared display label),
    but the actual visual component (waveform rendering, mesh viewer, EXIF
    grid layout) stays hand-written Svelte, not derived from the profile
    schema. This is a deliberate scope boundary: **profiles own validation
    and faceting; the component registry owns rendering.** Conflating the
    two would pressure the profile schema into carrying UI-layout metadata
    it has no business owning (exactly the DSL trap the prior-art survey
    warns against).

---

## 5. Provenance: extend `ItemVersion` or add a parallel table?

**Prior art: event-sourcing-lite patterns**
[Microsoft Learn: Event Sourcing pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing),
[AWS Prescriptive Guidance: event sourcing](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/event-sourcing.html)
- Canonical minimum event-record shape confirmed across sources: `event_id`,
  `stream_id` (the aggregate/entity the event belongs to), `event_type`,
  `data` (JSONB payload), plus timestamp/metadata fields. Filearr's
  **existing `ItemVersion` table already matches this shape almost exactly**:
  `id` (event_id), `item_id` (stream_id), `changed_at` (timestamp), `actor`
  (a metadata field), `patch` (JSONB data). **This is not a coincidence to
  discard — `ItemVersion` is already a lightweight event-sourcing-style log
  for the `user_metadata`/title/year/tags/external_ids write path.**
- The full event-sourcing pattern (rebuild current state by replaying all
  events) is explicitly **not** what Filearr does today — `Item` columns are
  the live state, `ItemVersion` is an audit trail alongside it, not the
  source of truth for reconstructing state. That's the right call to keep:
  full event-sourcing is a heavyweight architectural commitment (replay
  logic, snapshotting, versioned event schemas) that nothing in the roadmap
  scope actually requires. **"Event-sourcing-lite" for Filearr should mean
  "borrow the append-only audit-log shape," not "rebuild state via replay."**

**Key design question: key-level vs row-level provenance granularity.**
- Today's `ItemVersion.patch` already captures **which keys changed** within
  one write (it stores the diff dict, e.g. `{"user_metadata": {"rating": 5}}`),
  so key-level attribution for *user edits* already exists implicitly — you
  can inspect `patch` to know exactly which `user_metadata` key(s) a given
  `ItemVersion` row touched. What's **missing** is the extractor side: scans/
  extractors write `metadata_` directly via
  `item.metadata_ = {**item.metadata_, **meta}` with **no audit trail at
  all** — there is no record of "which scan run / extractor version wrote
  this specific key, and when." This is the real gap the roadmap's
  provenance ask is pointing at, more than the user-edit side (which is
  already reasonably served by `ItemVersion`).
- **Recommendation: two complementary mechanisms, not one, because they
  answer different questions and have very different volume/retention
  needs:**
  1. **Row-level, low-cardinality provenance columns directly on `Item`**
     (the scope's literal ask: "source agent, first/last seen, replication
     seq, policy version") — these are **current-state facts about the row**,
     not a history of changes, so they belong as plain columns, not as
     append-only log entries. `first_seen`/`last_seen` already exist on
     `Item`; add `source_agent_id` (nullable FK/text — nullable because v1
     is single-node local scanning, no distributed agents yet, per roadmap
     §1 not being built yet), `replication_seq` (nullable bigint — same
     forward-compat nullability reasoning, meaningless until §1's outbox/seq
     design lands), and `policy_version` (nullable text/int — records which
     library scan-config version produced the current extracted state,
     useful even in v1 for "this item was scanned under an older
     include/exclude policy, does it need a rescan" questions). **All
     nullable, all v1-inert, all forward-compatible with §1** without
     requiring §1 to exist first — exactly the same "ship the column now,
     wire the producer later" pattern the codebase already uses elsewhere
     (e.g. `hash_full_max_bytes` nullable-override pattern in `Library`).
  2. **Extend `ItemVersion` (do not fork a parallel table) to also cover
     extractor-driven `metadata_` writes**, with a new `source` discriminator
     column: `actor` today conflates "which human/API key" with an implicit
     assumption of human-driven change; add a lightweight
     `source: Mapped[str]` (`"user"` | `"scan"` | `"extract:<media_type>"` or
     similar convention) so a single unified audit table answers both "who
     changed `user_metadata`" and "which extractor run last touched
     `metadata_`" queries without maintaining two schemas, two retention
     policies, and two query surfaces. **Why extend rather than add a
     parallel table:** the roadmap explicitly notes "ItemVersion table
     exists for user edits — extend vs parallel table" as the open question;
     the event-sourcing-lite research above confirms `ItemVersion`'s shape
     is already the right generic shape for *any* attributed write, user or
     extractor — a parallel table would just duplicate columns
     (id/item_id/timestamp/patch) for no structural benefit, and would
     complicate the "Raw" tab's provenance display (two tables to join
     instead of one to query).
  - **Volume caveat, addressed by policy not schema:** every extract job
    already runs per-item, potentially on every rescan; writing an
    `ItemVersion` row on every extractor write would multiply table volume
    far beyond today's "only on explicit API PATCH" cadence. **Mitigate with
    a write-only-on-actual-change guard** (compare new `meta` dict against
    the previous `metadata_` before committing an `ItemVersion` row — skip
    if the extractor re-derived byte-identical values, which is the common
    steady-state rescan case) plus a **retention/purge policy** analogous to
    the existing recycle-bin purge (invariant 4) — e.g. collapse/purge
    extractor-sourced `ItemVersion` rows older than N days or keep only the
    latest per (item, source) pair, while **user-edit rows (source="user")
    are retained indefinitely** (they are the audit trail regulators/users
    actually care about; extractor churn is comparatively low-value once
    superseded). This asymmetric retention policy is the key scale mitigation
    and should be an explicit, documented decision (a periodic Procrastinate
    purge task, mirroring the existing recycle-bin purge task's shape) —
    not deferred as an afterthought, since unbounded per-rescan audit rows
    would be a real storage-growth regression risk on a large library with
    frequent scans.

---

## 6. Recommended architecture

### 6.1 Core decision: profiles validate `metadata_`; custom fields live in `user_metadata`

This is the single most important architectural call in this brief, so it is
stated first and justified explicitly against the DSpace/Paperless precedent
(§1) and the existing invariant 2 (extracted vs edited separation):

- **Metadata profiles** are **code-shipped, versioned schemas keyed by
  `media_type`**, describing the well-known fields each extractor emits
  (audio's `artist`/`album`/`genre`/..., video's `video_codec`/`resolution`/
  `hdr`/..., etc. — the vocabularies already documented in each extractor's
  module docstring today). A profile's job is to **validate `metadata_`
  writes** (the extractor's own output) against a declared shape — catching
  a future extractor regression (e.g. a parser accidentally emitting
  `"year": "not-a-number"`) before it lands in JSONB, and to **declare which
  of those fields are facetable/sortable** for the Meili projection (§3).
  Profiles are admin-*visible* (read-only listing via API, so the UI/Raw tab
  can label/order known fields nicely) but **not admin-editable** — exactly
  DSpace's "never edit the protected schema" rule, because profiles are
  coupled 1:1 to extractor code and editing one without editing the
  extractor would desync validation from what's actually produced.
- **Custom fields** are the **admin-defined, freeform extension point**,
  matching the roadmap's explicit ask ("central definition, per-location
  applicability") and Paperless's proven shape (central `CustomField`
  definition + typed value). Custom field *values* are written **only into
  `user_metadata`** — never `metadata_` — for three compounding reasons:
  1. **Invariant 2 is absolute** ("Scans/extractors may only write
     `metadata`; API edits only `user_metadata`"): a custom field is by
     definition something a human/admin defines and a human/API call
     populates (or, later, an automation acting through the write-scoped
     API) — it is never scan-derived, so it belongs on the edit side of the
     wall by the existing rule, with zero exception needed.
  2. **It matches the DSpace `local`-namespace analogy exactly**: a
     rescan/extractor upgrade must never silently clobber an admin's custom
     field the way it could if custom fields shared a column with
     scan-owned data — putting custom fields in `user_metadata` gives this
     guarantee for free, because extractors literally cannot write there.
  3. **It keeps `effective_metadata`'s existing overlay semantics correct
     without change**: `{**metadata_, **user_metadata}` already means a
     custom field naturally "wins" over any same-named extracted key without
     new merge logic, and a custom field can be added/removed/edited via the
     exact same `PATCH /items/{id}` `user_metadata` merge path that exists
     today (null clears a key, non-null sets it) — no new API verb needed
     for the *value*-write path, only for the field-*definition* CRUD (§6.3).
- **Validation therefore has two distinct call sites, both funneling through
  one shared validator function (per the Paperless #7361 lesson in §1):**
  profile validation runs inside `extract_item()` right before the
  `item.metadata_ = {**item.metadata_, **meta}` merge (reject/flag, never
  silently drop — an extractor validation failure should behave like any
  other extractor failure, i.e. recorded via the existing `_extract_error`
  convention, never raised in a way that kills the job); custom-field
  validation runs inside the existing `PATCH`/batch endpoints' `user_metadata`
  merge path, validating only the keys that correspond to a registered
  custom-field definition (a `user_metadata` key with no matching custom-
  field definition is accepted as-is, unvalidated freeform data — this
  preserves full backward compatibility with today's "any key, any value"
  behavior for ad hoc metadata that never gets formalized into a field
  definition).

### 6.2 Schema changes (tables/columns)

```
-- New: metadata profile registry (code-shipped, read-mostly; NOT admin-editable
-- field lists, but the row itself IS how the app tracks "what version of the
-- video profile validated this item's metadata_").
CREATE TABLE metadata_profiles (
    id            uuid PRIMARY KEY DEFAULT uuidv7(),
    media_type    text NOT NULL UNIQUE,   -- FK-like to MediaType enum values
    version       integer NOT NULL,        -- bump on any field-shape change
    schema        jsonb NOT NULL,          -- {field_name: {type, required, facetable, sortable, label}}
    created_at    timestamptz NOT NULL DEFAULT now()
);
-- Seeded/upserted at app startup from code-defined profile modules (mirrors how
-- MediaType/HashPolicy are code enums, not DB-editable) -- migrations only ever
-- ADD rows or bump `version`; no admin DELETE/UPDATE endpoint for `schema` itself.

-- New: admin-defined custom field definitions (Paperless CustomField-shaped).
CREATE TABLE custom_fields (
    id              uuid PRIMARY KEY DEFAULT uuidv7(),
    name            text NOT NULL UNIQUE,       -- the user_metadata key this governs
    label           text NOT NULL,               -- display label
    data_type       text NOT NULL,               -- string|integer|float|boolean|date|url|select
    select_options  text[],                       -- only when data_type = 'select'
    applies_to      text[] NOT NULL DEFAULT '{}', -- media_type(s); empty = all types
    library_ids     uuid[] NOT NULL DEFAULT '{}', -- per-location applicability; empty = all libraries
    facetable       boolean NOT NULL DEFAULT false,
    sortable        boolean NOT NULL DEFAULT false,
    required        boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- data_type is immutable after creation (Paperless precedent, §1) --
    -- enforced at the API layer (reject data_type change on PATCH), not by a
    -- DB trigger, to keep the "why" surfaced as a normal 422 error.
);

-- Item gains provenance columns (all nullable -- v1-inert, forward-compatible
-- with roadmap §1's distributed-agent work which will populate them later).
ALTER TABLE items
    ADD COLUMN source_agent_id  uuid,       -- NULL in v1 (local-only scanning)
    ADD COLUMN replication_seq  bigint,     -- NULL until §1's outbox exists
    ADD COLUMN policy_version   text;       -- library scan-config version at last extract

-- ItemVersion extended with a source discriminator (extend, not fork --
-- see §5). Backward compatible: existing rows get source='user' via a
-- one-time backfill (they were all API-driven edits already).
ALTER TABLE item_versions
    ADD COLUMN source  text NOT NULL DEFAULT 'user';
-- New retention purge task (periodic, mirrors the existing recycle-bin purge
-- shape) deletes source != 'user' rows past a retention window / collapses
-- to latest-per-(item_id, source). 'user' rows are never auto-purged.

-- Defense-in-depth structural CHECK (no pg_jsonschema; native jsonb_path_exists,
-- PG12+, no extension). Cheap sanity check, NOT full field validation.
ALTER TABLE items
    ADD CONSTRAINT user_metadata_is_object
    CHECK (jsonb_typeof(user_metadata) = 'object');
```

- **No raw embedding-vector-style column is introduced here** — this brief
  intentionally does not touch the sibling phase-3 brief's "vectors live only
  in Meili" rule; provenance/profile data is all Postgres-resident because,
  unlike embeddings, it is cheap, small, and not regenerable from anything
  else (it *is* the source of record).
- **GIN index gap noted, not fixed silently:** `user_metadata` currently has
  no GIN index (only `metadata` does) — once custom-field values start
  living there in volume and need filtering/search, add
  `Index("ix_items_user_metadata", "user_metadata", postgresql_using="gin")`
  as part of this work (small, uncontroversial addition, but explicitly
  called out since it's easy to miss when only `metadata` "feels" like the
  performance-sensitive column today).

### 6.3 API surface

- **Profile endpoints (read-only, any authenticated `read` scope):**
  `GET /api/metadata-profiles` (list all), `GET /api/metadata-profiles/{media_type}`
  (one profile's current schema) — powers the frontend's "key facts"
  ordering/labeling (§4) and any future admin UI that wants to *show* what
  fields a type produces. No POST/PATCH/DELETE — profiles are code-owned.
- **Custom field CRUD (new `admin` scope, matching the existing
  read/write/admin scope model in `security.py`):**
  - `POST /api/custom-fields` — create (name/label/data_type/applies_to/
    library_ids/facetable/sortable/required). `data_type` immutable after
    creation (§6.2) — a later PATCH attempting to change it returns 422 with
    a clear message, not a silent no-op.
  - `GET /api/custom-fields` — list (filterable by `applies_to`/`library_id`
    for the frontend to know which fields are relevant to the item currently
    being viewed/edited).
  - `PATCH /api/custom-fields/{id}` — edit label/applicability/facetable/
    sortable/required (never `data_type`, never `name` — renaming would
    orphan existing `user_metadata` values under the old key; if a rename is
    truly needed, model it as create-new + a separate, explicit data-
    migration action, not a field-definition PATCH).
  - `DELETE /api/custom-fields/{id}` — **soft-behavior only**: removes the
    definition (so it stops being offered/validated/faceted going forward)
    but **does not touch existing `user_metadata` values** already written
    under that key on any item (matches invariant 4's "never hard-delete
    data" spirit even though this is a field definition, not an item —
    the safer default when the blast radius of a mistake is "silently wipes
    user data across every item").
  - Every mutating call gated behind `require_scope("admin")`, consistent
    with how library-management endpoints are already scoped.
- **Validation error shape (both profile and custom-field validation paths
  share this):** a 422 response whose body mirrors Pydantic's native
  `ValidationError.errors()` structure (already FastAPI's default for schema
  validation elsewhere in this codebase) — `{"detail": [{"loc": [...],
  "msg": ..., "type": ...}]}` — extended with a Filearr-specific
  `"field_source": "profile"|"custom_field"` tag per error so a single PATCH
  that touches both a profile-covered extracted field (unlikely via the API,
  since extractors alone write `metadata_` — mainly relevant to the internal
  extractor-side validation call) and a custom field can distinguish which
  layer rejected which key.
- **Existing `PATCH /items/{id}` / `POST /items/batch` behavior is
  unchanged in shape** — `user_metadata` remains a `dict[str, Any]` at the
  Pydantic-schema level (so ad hoc, non-formalized keys keep working exactly
  as today); the *new* validation is an additional pass applied only to keys
  that match a registered `custom_fields.name`, not a tightening of the
  existing endpoint contract. This is a deliberately non-breaking change.

### 6.4 Meilisearch projection strategy

- `search.py:build_doc()` gains a **generic custom-field projection loop**:
  for every `custom_fields` row with `facetable=true` (or `sortable=true`),
  read the corresponding key out of `effective_metadata` and project it under
  a stable, collision-safe attribute name (e.g. prefix `cf_<name>` to avoid
  ever colliding with a hardcoded static field like `genre` or a future
  profile field) into the Meili document. Fields with neither flag set are
  **not** projected into Meili at all (they remain visible via
  `effective_metadata`/the Raw tab and PATCH responses, just not searchable/
  filterable) — keeping the common case cheap, per §3's churn-avoidance
  recommendation.
- `FILTERABLE`/`SORTABLE` attribute lists become **partly dynamic**: computed
  at `ensure_index()`/`rebuild_index` time as `STATIC_FILTERABLE +
  [f"cf_{f.name}" for f in facetable_custom_fields]` (queried from
  `custom_fields` at rebuild time), rather than the current hardcoded Python
  list. **Every settings update this produces must go through the rebuild-
  and-swap path (§3), never an in-place `update_filterable_attributes()`
  call against the live index** — this is the one behavioral change to
  `ensure_index()`'s current call pattern this brief requires.
- **Type coherence for faceting:** a custom field's declared `data_type`
  (integer/float/boolean/date/string/select) should map to a consistent
  Meili-side representation (e.g. `date` → epoch integer, matching how
  `mtime` is already projected as `int(item.mtime.timestamp())` in
  `build_doc()` today) so Meili's inferred facet type stays stable across
  a rebuild regardless of which items happen to be indexed first — directly
  addressing the "facet type inferred from data order" risk flagged in §3.
- **No profile fields need new Meili projection logic beyond what already
  exists** — profiles primarily add *validation*, and the fields they cover
  (codec, resolution, genre, etc.) are already flowing into `build_doc()`
  via the existing `meta.get(...)` calls; the profile's `facetable`/
  `sortable` declarations should simply become the **documented source of
  truth** for which of those existing hardcoded `build_doc()` lines exist,
  rather than a parallel undocumented list — a cleanup, not a new mechanism.

---

## 7. Task breakdown

Continuing the T-numbering convention from `docs/phase-1-scanner-tasks.md`
and the phase-3 brief's T20-T33 range — this brief uses **T34+**.

| # | Task | Size | Accept criteria |
|---|---|---|---|
| T34 | `metadata_profiles` table + code-defined profile modules per `MediaType` (audio/video/image/audiobook/model3d/document/spreadsheet), seeded/upserted at startup | M | Every existing extractor's documented key vocabulary (per module docstring) has a matching profile entry; `GET /api/metadata-profiles` returns all seven |
| T35 | Shared validator function; wire into `extract_item()` before the `metadata_` merge, `_extract_error`-style failure reporting on violation | M | A deliberately malformed extractor output (wrong type for a declared field) is caught, recorded via `_extract_error`, and does not corrupt `metadata_` with an invalid value |
| T36 | `custom_fields` table + admin-scope CRUD API (`POST`/`GET`/`PATCH`/`DELETE /api/custom-fields`), `data_type` immutability enforced at API layer | M | Creating a field with an invalid `data_type` value 422s; attempting to PATCH `data_type` or `name` after creation 422s with a clear message; soft-delete leaves existing `user_metadata` values untouched |
| T37 | Custom-field value validation wired into `PATCH /items/{id}` and `POST /items/batch` `user_metadata` merge path, shared validator reused from T35 | M | A `user_metadata` key matching a registered custom field with `data_type=integer` rejects a string value with a structured per-field 422; unregistered keys still pass through unvalidated (no behavior change for ad hoc keys) |
| T38 | GIN index on `items.user_metadata`; defense-in-depth `CHECK (jsonb_typeof(user_metadata) = 'object')` constraint | S | Migration applies cleanly against the existing baseline; a raw SQL attempt to set `user_metadata` to a non-object is rejected at the DB layer |
| T39 | Dynamic custom-field Meili projection (`cf_<name>` attributes) + rebuild-and-swap-only settings update path in `ensure_index()`/`rebuild_index` | M | Toggling a custom field's `facetable` flag results in it appearing as a Meili filterable attribute only after a rebuild+swap cycle, never via a live in-place settings call; facet type is stable across repeated rebuilds regardless of item indexing order |
| T40 | Provenance columns on `Item` (`source_agent_id`, `replication_seq`, `policy_version`, all nullable) + populate `policy_version` from the current library scan-config at extract time | S | New columns exist and are nullable; a fresh extract populates `policy_version` with a value that changes when the owning library's scan-relevant config changes |
| T41 | Extend `ItemVersion` with `source` discriminator column + backfill existing rows to `source='user'`; extractor writes to `metadata_` create a `source='scan'`-or-`'extract:<media_type>'` version row **only when the value actually changed** | M | Backfill migration sets `source='user'` on 100% of pre-existing rows; a rescan that reproduces byte-identical `metadata_` creates zero new version rows; a rescan that changes a value creates exactly one attributed row |
| T42 | Retention/purge policy for non-`user` `ItemVersion` rows (periodic Procrastinate task, mirrors existing recycle-bin purge shape); `user`-sourced rows explicitly exempt | S | A configurable retention window purges old `source != 'user'` rows; rows with `source='user'` are never touched by the purge task regardless of age |
| T43 | Frontend: per-`media_type` card + detail Svelte component registry (image/audio/video/audiobook/model3d/document/spreadsheet + generic fallback), wired via native Svelte-5 dynamic-component syntax (no `<svelte:component>`) | L | Each media type renders its dedicated component; an unregistered/future media type falls back to the generic "key facts" component without error |
| T44 | Frontend: always-available "Raw" tab (core columns + `metadata_` + `user_metadata` + provenance fields, flat key/value table) | S | Every stored field for a given item is visible somewhere in the Raw tab, including provenance columns from T40 and both metadata columns unmerged (shown separately, not only as `effective_metadata`) |
| T45 | Frontend: custom-field-aware rendering in the generic/key-facts component (order/label fields per profile/custom-field definition where available) | M | A custom field with a declared `label` renders using that label, not its raw key name; field order follows the profile's/custom-field's declared order where declared |

Sizes: S = part of a normal sprint slice, M = multi-day with real design
decisions, L = major item needing its own design pass (matches the sizing
convention already used in `docs/future-roadmap.md` and the phase-3 brief).

**Suggested sequencing:** T34→T35 (profiles) and T36→T37 (custom fields) can
proceed in parallel — they touch different tables and different write paths.
T38 is a small prerequisite for T39. T40/T41/T42 (provenance) are independent
of the profile/custom-field work and can slot in anytime. T43/T44/T45
(frontend) depend on T34/T36 existing (at least the read APIs) but not on
T35/T37/T38-42 — the Raw tab (T44) in particular has almost no backend
dependency beyond fields that already exist today and could ship first as a
quick win.

---

## 8. Open questions (need a maintainer decision, not further research)

1. **Custom-field `library_ids` applicability granularity.** The roadmap
   scope says "per-location applicability" — this brief models that as an
   array of library UUIDs on the field definition (empty = all libraries).
   Confirm this is the right granularity vs. a coarser "per-library-group"
   concept that might make more sense once roadmap §1's machine-groups
   concept exists — recommend shipping the simple per-library array now
   (matches the include/exclude-globs per-library pattern already in
   `Library`) and revisiting only if/when groups land.
2. **Profile schema versioning/migration story.** `metadata_profiles.version`
   is proposed as a bare integer bumped on any field-shape change, but there
   is no described mechanism yet for what happens to *already-extracted*
   items when a profile version bumps (e.g. a field is renamed or its type
   changes in a new extractor release) — does a version bump trigger a
   recommended rescan, a background migration of existing `metadata_`
   values, or nothing (old items just show old-shaped data until next
   rescan)? Recommend the last option (do nothing automatic — rescans are
   already the existing mechanism for refreshing extracted data) but this
   should be an explicit, confirmed decision, not an implicit default.
3. **Custom-field `required` enforcement scope.** T36 proposes a `required`
   flag but this brief does not resolve what enforces it — is a "required"
   custom field enforced at PATCH time (reject clearing/omitting it), at
   scan/display time (just a UI hint, "missing required field" badge), or
   both? Paperless's own conditional-validation discussion (#10514) shows
   even mature prior art hasn't solved the general case (required *only
   when* some other condition holds) — recommend shipping `required` as a
   **display-only hint** in v1 (no hard API-level enforcement) and revisit
   if real usage demands stricter enforcement.
4. **`pg_jsonschema` license re-verification.** This brief recommends
   against adding it (application-side Pydantic validation instead), but
   flags that its exact license was not independently confirmed in this
   research pass. If a future maintainer reconsiders DB-side JSON Schema
   validation for any reason, re-verify the license explicitly before
   adoption — do not rely on this brief's absence of a citation as
   confirmation either way.
5. **Namespace-prefix convention for extracted metadata keys.** §1 flags a
   real (if currently latent) risk of key collisions across extractors
   (e.g. a future EXIF-deep-extraction pass and the existing image/video
   extractors both wanting a `camera` key with possibly different shapes).
   This brief recommends flat keys today with profiles as the collision
   boundary but does not mandate a `namespace_key` convention (e.g.
   `exif.camera` vs `camera`) — decide whether to adopt such a convention
   now (cheap to do before more extractors exist) or defer until a real
   collision is observed (roadmap §5 P2's EXIF work is the first concrete
   trigger — flag this brief's finding to whoever picks up that task).
6. **CKAN/NocoDB/Baserow findings are weaker than the other citations in
   this brief** (see §1) — if a maintainer wants a stronger comparative
   basis for the custom-field data model before implementation, a
   follow-up research pass specifically on CKAN's `extras`/schema.json
   mechanism and NocoDB's/Baserow's actual field-storage internals
   (not just their marketing comparisons) would close that gap. Not
   blocking — Paperless-ngx and DSpace alone provide a sufficient,
   well-sourced basis for the recommendation made here.
