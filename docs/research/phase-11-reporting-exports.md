# Phase 11 Research — Reporting & Export Templates

Sonnet 5 research brief, 2026-07-07. Feeds `docs/tasks/phase-11-*-tasks.md`
scaffolding once reviewed (`docs/execution-plan.md` per-phase workflow).
Decision priority: **security > integrity > reliability > speed >
compatibility > scalability**. AGPL-compatible OSS only. **No new runtime
dependency proposed** — `xlsxwriter==3.2.9` is already pinned.

## 0. Scope and relationship to prior phases

Covers canned + custom **reports and bulk exports** over the catalog: largest
files, low-quality-video candidates, duplicates, full/filtered catalog dumps.
`docs/phase-1-scanner-tasks.md` deferred "exports" as a Phase 4 non-goal from
the original plan; this is that work, under the roadmap's Phase 11 slot.

Adjacent phases already own pieces that MUST be referenced, not duplicated:
- **Phase 3** (`docs/research/phase-3-search-findability.md`, P3-T7/P3-T10):
  `saved_searches` table and the duplicate-awareness "N copies" badge
  (content_hash/quick_hash groups). Reports reuse both.
- **Phase 4** (P4-T3): `custom_fields` (name/label/data_type/facetable/
  sortable) — report column candidates, not redefined here.
- **Phase 7** (`backend/filearr/querydsl.py`, phase-7 research §1.4/§4): the
  normative filter grammar (`kind:`/`ext:`/`size:`/`modified:`/`created:`/
  `path:`/`tag:`/`hash:`). Reports reuse this grammar, not a second one.
- **Phase 8** (`alert_channels`: webhook/email/apprise) is the delivery
  mechanism for scheduled reports (§4.4). No new channel type.

`models.py` invariants that constrain this design: `metadata_` (extracted) vs
`user_metadata` (edits) are separate JSONB columns; effective value is the
user overlay (`Item.effective_metadata`); identity is `(library_id,
rel_path)`; scans never hard-delete. Exports must preserve overlay semantics
and are themselves disposable snapshots, never a second source of truth.

`backend/filearr/tasks/ffprobe.py`'s normalised schema (container, duration,
bitrate, video_codec, width, height, resolution, frame_rate, hdr, hdr_format,
color_primaries, color_transfer, audio_codec, audio_tracks, subtitle_tracks)
is the **only** input to the low-quality heuristic (§3) — no new extraction.

---

## 1. Findings — canned media-library reports, prior art

**Tautulli** (Plex companion, most mature "library statistics" precedent):
global play-count stats plus a **Media Info table per library**, sortable by
size/bitrate/resolution/codec, and a dedicated **Exporter** dumping metadata/
media-info to CSV/JSON/XML per-library/collection/playlist. [Tautulli](https://tautulli.com/),
[Exporter Guide](https://github.com/Tautulli/Tautulli/wiki/Exporter-Guide).
Filearr has no "watched" concept, so the portable idea is the Media Info
table + exporter, not play-count reports.

**Jellyfin Reports plugin**: activity + media reports, exportable to **Excel
and CSV** natively. [jellyfin-plugin-reports](https://github.com/jellyfin/jellyfin-plugin-reports).
Confirms CSV+XLSX (not JSON-first) as the expected default shape.

**Stash** (closest architectural sibling — scan-and-catalog Postgres model):
a Scene Duplicate Checker HTML report with per-group actions (tag-to-exclude,
select-highest-quality, merge). Users have explicitly requested **PSNR/VMAF**
quality metrics in that report — a gap, not a shipped feature.
[Duplicate Checker](https://github.com/stashapp/stash/discussions/5415),
[quality-metrics request](https://github.com/stashapp/stash/issues/2397). Flag
as a v1 non-goal (frame-decode cost far beyond ffprobe metadata), but carry
the "surface quality columns so a human can pick which copy to keep" idea
into the duplicate-groups report (§8 item 6) using existing
resolution/bitrate/codec fields.

**beets** Smart Playlist plugin: playlists from beets' query syntax + a sort
directive (`bitrate+`). [docs](https://beets.readthedocs.io/en/stable/plugins/smartplaylist.html).
The "saved query + sort + materialized output" pattern in miniature — directly
analogous to §4's report-definition model.

**WizTree / TreeSize**: WizTree's File View is a flat, sortable-by-size list
with Export-to-CSV; its duplicate finder groups by content hash. TreeSize
supports MD5/SHA-256 duplicate comparison. [WizTree](https://www.diskanalyzer.com/guide),
[TreeSize dupes](https://www.jam-software.com/treesize/find-remove-duplicate-files.shtml).
Confirms "largest N" and "duplicates by hash" as the two universally-expected
canned reports across the whole product category.

**Paperless-ngx** (already the P4-T3 custom-fields precedent): ships **saved
views** (persisted filter + customizable display fields — exactly "saved
search + column set") but explicitly does **not** ship CSV/XLSX export of a
filtered list with custom fields — one of its most-requested missing features.
[saved-views PR](https://github.com/paperless-ngx/paperless-ngx/pull/6439),
[export request](https://github.com/paperless-ngx/paperless-ngx/discussions/6365).
Strong validation for §4's model: the data model is right, competitors just
never wired it to export.

Duplicate-UX prior art (czkawka/dupeGuru/Immich) is already fully researched
in `docs/research/phase-3-search-findability.md` §4 — not re-researched here;
the duplicate-groups report is a flat *export* of P3-T10's existing grouping,
not a new detection mechanism. Every surveyed tool treats destructive
follow-up (delete/re-acquire) as separate from the report itself, matching
invariant 6 (read-only mounts): **v1 reports are strictly read-only.**

---

## 2. Findings — "low-quality video" heuristics, prior art

**Radarr/Sonarr quality definitions** (via TRaSH-Guides) define, per
resolution/source tier, a **Minimum MB/min** floor:
[TRaSH quality settings](https://trash-guides.info/Radarr/Radarr-Quality-Settings-File-Size/)

| Quality tier | Min MB/min | Min Mbps | Min bits/px/s* |
|---|---|---|---|
| HDTV-720p | 17.1 | 2.28 | 0.00247 |
| WEBDL/WEBRip-720p | 12.5 | 1.67 | 0.00181 |
| Bluray-720p | 25.7 | 3.43 | 0.00372 |
| HDTV-1080p | 33.8 | 4.51 | 0.00218 |
| WEBDL/WEBRip-1080p | 12.5 | 1.67 | 0.00080 |
| Bluray-1080p | 50.8 | 6.78 | 0.00327 |
| Remux-1080p | 102 | 13.6 | 0.00656 |
| HDTV-2160p | 85 | 11.3 | 0.00137 |
| WEBDL/WEBRip-2160p | 34.5 | 4.60 | 0.00055 |
| Bluray-2160p | 102 | 13.6 | 0.00164 |
| Remux-2160p | 187.4 | 25.0 | 0.00301 |

\* bits/px/s ("BPP") = bitrate ÷ (width×height), derived here from TRaSH's
MB/min for §3's use (not published directly by TRaSH). WEB-DL sits far below
HDTV/Bluray/Remux at equal resolution because streaming sources are already
efficiently encoded — **floor is source/codec-dependent, not resolution-
alone**, so §3 codec-adjusts rather than using one flat number.

TRaSH's FAQ also states sub-720p is **categorically undesirable** on modern
displays — justifies a hard resolution-floor component separate from the
bitrate floor. Anime/animated content gets a deliberately wide-open table in
the same source (Open Question 5).

**Tdarr vs. Unmanic** license check (both already flagged in `CLAUDE.md` as
"closed-source, don't recommend") — **confirmed still accurate**: Tdarr is
closed-source under a non-OSI EULA; Unmanic is genuinely open source.
[2026 comparison](https://www.pistack.xyz/posts/tdarr-vs-unmanic-vs-handbrake-self-hosted-video-transcoding-guide-2026/).
Neither is adopted (Filearr doesn't transcode — invariant 6), but their rule
*concepts* (resolution floor, codec deny-list, bitrate floor) are what §3
formalizes as a pure scoring function over already-extracted ffprobe fields.

---

## 3. Low-quality-video scoring heuristic (concrete, v1)

Pure function over `Item.effective_metadata` — **no new ffprobe fields**.
Additive score, computed at report-run time (not persisted — a "quality
score" column would itself violate invariant 2, being neither extracted nor
edited data).

| Signal | Condition (existing ffprobe keys) | Points | Rationale |
|---|---|---|---|
| Sub-HD resolution | `height < 720` | +40 | TRaSH FAQ: sub-720p categorically undesirable (§2) |
| Legacy/obsolete codec | `video_codec` ∈ `{mpeg1video, mpeg2video, msmpeg4v2, msmpeg4v3, h263, wmv1, wmv2}` | +25 | Matches *arr community legacy-codec blocklists; conservative — excludes bare `mpeg4` since ffprobe's `video_codec` can't distinguish DivX/Xvid from generic MPEG-4 ASP without `codec_tag_string` (not currently captured, Open Question 5-adjacent) |
| Bitrate-per-pixel below floor | `bitrate/(width*height)` below §2's resolution-tier floor, floor **halved** if `video_codec` ∈ `{hevc, av1, vp9}` (standard ~2x efficiency vs h.264/mpeg2) | 0–25, linear (≥50% below floor = full 25) | Directly derived from TRaSH's MB/min minimums |
| HDR/color-metadata mismatch | `hdr=true` but `color_transfer`/`color_primaries` absent or SDR-typical (`bt709`), or the reverse on a `height>=2160` item | +10 | Flags likely mislabeled/incorrectly-remuxed HDR |
| Audio downmix oddity | `height>=2160` and every `audio_tracks[].channels<=2` with none lossless (`truehd`/`dts`/`flac`) | +10 | Stereo-only-for-4K suggests a re-encoded, not original, source |

**Bands** (config-tunable, not hardcoded): `score>=50` → re-acquisition
candidate; `25–49` → review; `<25` → ok. Implementation:
`score_item(effective_metadata: dict) -> ScoreResult` — pure, no I/O,
deterministic, mirroring `querydsl.py`'s own design philosophy; returns which
components fired so a report column can show *why*, not just a number.

---

## 4. Report definition model — recommendation

**Unify, don't fork**: a report is `(query, columns, sort, format[,
schedule])`. Two flavors share one execution engine.

- **Canned = code, not DB rows.** Python functions in a registry
  (`filearr/reports/canned.py`) returning `(filter_expr, columns,
  default_format, label)`. Zero migrations; light parameterization (N,
  library, media_type via query params) — **not** full querydsl — keeps
  canned reports simple and impossible to break via a malformed filter.
- **Custom = `report_definitions` table** (§9): name, filter (inline
  querydsl string **or** a reference to an existing `saved_searches` row —
  mutually exclusive via CHECK, so an already-saved search need not be
  retyped), ordered column spec, sort, format, optional cron + channels.

Directly mirrors Paperless-ngx's "saved view = filter + display fields" (§1),
just wiring the export Paperless itself never shipped.

**Column spec** prefix scheme, aligned with invariant 2's overlay:
- bare core column (`filename`, `size`, `mtime`, `media_type`, `library`,
  `path`, `tags`, …) → typed `Item` column.
- `m.<key>` → `metadata_[key]` only (extracted).
- `u.<key>` → `user_metadata[key]` only (edits).
- bare `<key>` matching a `custom_fields.name` → **effective** value
  (`Item.effective_metadata[key]`) — matches what the API/UI show; `m.`/`u.`
  remain available to audit scanner vs. override side by side.

Values are read off the **already-fetched** row in Python (a small safe
dotted-path getter) — never via string-built JSON-path SQL; all JSONB SQL
access stays on the query-filter side (§7), never the column-selection side.

**Scheduling** rides `library.scan_cron` exactly: static every-minute tick +
`cronsim` (T5's user-confirmed architecture) — no new scheduling mechanism.
**Delivery** rides Phase 8's `alert_channels` (webhook/email/apprise)
unchanged: run job → hand artifact to a channel driver. Email attaches the
artifact (size ceiling, Open Question 2); webhook gets a signed short-lived
link rather than embedding large file bytes.

---

## 5. Filter grammar reuse — querydsl.py

`backend/filearr/querydsl.py` is Phase 7's normative reference parser (R6:
single source of truth, Go port must match `shared/querydsl-vectors.json`
byte-for-byte). Its grammar (`kind:`/`ext:`/`size:`/`modified:`/`created:`/
`path:`/`tag:`/`hash:`, implicit-AND, negation, explicit-fuzzy) is reused
**verbatim** for `report_definitions.query` and canned-report composition —
the same string typeable into the future local-CLI/web-UI or the in-app
saved-search box works unchanged in a report definition.

This needs one new piece: **`querydsl_to_sqla(query: Query) -> ColumnElement`**,
mapping the parsed AST to SQLAlchemy expressions against typed `Item` columns
(`kind`→`media_type`, `ext`→`extension`, `size`→`size`, `modified`/`created`→
`mtime`/`first_seen`, `path`→glob-to-`ILIKE`/regex, `tag`→`tags.any()`,
`hash`→`quick_hash`/`content_hash` prefix). Nothing in Phases 2–9 built this
Postgres compiler (Phase 7's scope was CLI/web-UI parity, not SQL
translation) — Phase 11 owns it (P11-T1).

**Metadata-matching exports need more than today's grammar** — filtering on
`metadata.resolution` or a custom field isn't expressible in the current
8-key grammar. Rather than fork a second grammar, extend it with a new key
family — `meta.<key>` (extracted) / `cf.<key>` (custom field, effective) —
co-owned with whoever holds Phase 7 next, since `querydsl-vectors.json` is a
release-blocking cross-language artifact (R6) needing new vectors in the
same commit as new keys, both languages. Flagged as Open Question 1, not
decided unilaterally here.

---

## 6. Streaming export pipeline

### 6.1 Formats

| Format | Mechanism | When |
|---|---|---|
| CSV | stdlib `csv.writer`, one row/generator step | default; universal |
| NDJSON | `json.dumps(row)+"\n"` per row | scripting/piping; the only large-export JSON option |
| XLSX | `xlsxwriter.Workbook(path, {'constant_memory': True})` | explicit `.xlsx` request; already-pinned dep |
| JSON (array) | buffered, small-path only | tooling expecting `[{...}]`; never for large/async path |
| Parquet | **not adopted** | see §6.4 |

`constant_memory` verified: flushes each row after the next is written, so
peak memory ≈ one row regardless of total rows, at roughly normal-mode
performance; rows must be written in strict sequential order, and
`add_table()`/post-write cell edits/autofit don't work — irrelevant for a
flat tabular dump. [XlsxWriter memory docs](https://xlsxwriter.readthedocs.io/working_with_memory.html).

### 6.2 Streaming from Postgres

SQLAlchemy 2 async `AsyncSession.stream()`/`AsyncConnection.stream()` with
`execution_options(yield_per=500..1000)` uses a **server-side cursor**,
fetching fixed-size batches instead of materializing the full result —
the "never load 1M rows in RAM" requirement, trading round-trip overhead for
memory boundedness (correct trade per the standing priority order).
[SQLAlchemy 2.0 asyncio docs](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html).

### 6.3 Sync vs. background-job threshold

- **Below `FILEARR_EXPORT_SYNC_MAX_ROWS`** (proposed default ~10,000): serve
  directly via FastAPI `StreamingResponse` + `Content-Disposition: attachment`
  — no job, no artifact file, no polling. Covers nearly all library/filter-
  scoped canned reports.
- **Above threshold, or any scheduled report**: Procrastinate job (dedicated
  export queue, §7) writes a server-local artifact (never under a web-served
  static root), tracked in `report_runs` (§9); client polls or reuses the
  existing SSE-progress pattern (T4), then downloads once `complete`.
- Threshold decision uses a cheap bounded `COUNT` with its own timeout —
  never by materializing the full result first.
[FastAPI 1M-row CSV pattern](https://medium.com/@connect.hashblock/serving-1m-csv-exports-with-fastapi-and-streaming-responses-without-memory-bloat-32405f42cff5).

### 6.4 Parquet/pyarrow — assessed and rejected for v1

`pyarrow` adds roughly **120 MB** to an install (vs ~70 MB for numpy+pandas
combined); Parquet-reading alone has been measured to double a pandas
environment's footprint; Arrow maintainers have an open, unresolved effort to
let a minimal install skip libparquet entirely.
[pyarrow footprint](https://uwekorn.com/2020/09/08/trimming-down-pyarrow-conda-1-of-x.html),
[pandas PDEP-10](https://pandas.pydata.org/pdeps/0010-required-pyarrow-dependency.html),
[apache/arrow#39006](https://github.com/apache/arrow/issues/39006). Conflicts
with "minimal new deps" and the project's existing pattern of a short,
justified dependency list. **Recommendation**: skip parquet for v1 — CSV/
XLSX/NDJSON cover every realistic consumer; if a real columnar-analytics
consumer emerges, ship as an optional extra (`filearr[parquet]`), never a
base dependency.

---

## 7. Security notes

**JSONB predicate injection.** Never build a WHERE clause via string
formatting of a user-supplied key/value (never `text(f"metadata->>'{key}'")`).
SQLAlchemy's own accessors (`Item.metadata_[key].astext`, `.cast(Numeric)`,
`==`, `.in_()`) parameterize the *value*; the *key* (part of the JSON path,
not a bound value) must be allow-list validated before use — `cf.<key>` keys
must exist in `custom_fields.name` (already constrained by P4-T3's
`normalize_field_name()`); `meta.<key>` keys must match a conservative
pattern (`^[a-z][a-z0-9_.]*$`), rejected at **parse time** in the querydsl
extension (§5), before reaching any query builder. Numeric comparators go
through `.astext.cast(Numeric)` in a try/except mapped to 422 — never manual
string-to-SQL splicing. [SQLAlchemy JSONB filtering discussion](https://github.com/sqlalchemy/sqlalchemy/discussions/7991).

**RBAC gating (rides Phase 6).** Bulk metadata export is data-exfiltration-
shaped even though "just metadata" — all report/export endpoints require the
`download` action (phase-6's grantable-actions list: `search_metadata,
search_content, download, upload, modify, delete, edit_metadata,
manage_alerts`), not merely `search_metadata`. Pre-RBAC, fall back to the
existing scope model (`admin`/`write`, matching P4-T3's custom-field CRUD).

**Path-scoped ACL.** Once Phase 6's ltree path-grant model lands, a principal
with a partial-tree grant must never see rows outside it from *any* report.
Compile the caller's allowed path prefixes (phase-6 §2.5's non-search-path
evaluation branch, already used for item-detail/PATCH/download) into one
additional predicate appended to the report's compiled filter — a single
compiled statement, not a per-row function call (reliability requirement:
must not silently under/over-filter under load).

**Resource limits.**
- `FILEARR_EXPORT_MAX_ROWS` (default 1,000,000): reject before query
  execution if the cheap row-count estimate exceeds it.
- `FILEARR_EXPORT_SYNC_MAX_ROWS` (default ~10,000): sync/async routing (§6.3).
- A **dedicated Procrastinate queue** for exports, separate from scan/extract,
  so one large export never starves scanning capacity.
- A per-principal concurrent-export cap to prevent trivial DoS.
- `FILEARR_EXPORT_TTL_HOURS` (default 24–72h) + a periodic purge riding the
  T5-tick mechanism; the `report_runs` row is retained (audit trail,
  matching Phase 8's event-retention posture) with only the artifact file +
  a `purged_at` marker affected.
- Artifacts live outside any web-served static root; downloads require the
  same auth as the original request — **no permanently bookmarkable
  `?token=` URL** (phase-7 §2.4's Jupyter-pattern caution applies equally);
  a short-lived signed link, if needed for webhook/cross-device delivery,
  expires quickly and is scoped to one artifact.
- Crash handling mirrors invariant 7 (crashed ScanRun → `failed`, never left
  `running`): a reconcile sweep flips stale `report_runs.status='running'`
  rows past a timeout to `failed`, matching Phase 9's reconcile precedent.

---

## 8. Canned report list v1

1. **Full catalog export** — all active items, core columns +
   `effective_metadata` flattened. Universal baseline (Tautulli/Jellyfin/
   Paperless "export everything").
2. **Largest N files** (overall/per-library/per-media_type) — WizTree/
   TreeSize's headline feature; trivial `ORDER BY size DESC LIMIT N`.
3. **Recently added** (`first_seen` window) — Tautulli/Jellyfin precedent.
4. **Recently modified** (`mtime` window) — activity/drift tracking, same
   shape as #3.
5. **By-codec × by-resolution matrix** — inventory `GROUP BY`, beets-style
   aggregate, cheap query.
6. **Duplicate groups** — flat export of P3-T10's `content_hash` grouping
   (group id, copy count, wasted bytes, path/library/last-seen + quality
   columns per copy — answers the gap Stash's users flagged in §1, no VMAF/
   PSNR pipeline needed). Reuse P3-T10's aggregate helper once it exists
   (Open Question 6).
7. **Low-quality re-acquisition candidates** — §3's scored heuristic,
   sortable by score and by which component fired. The one canned report
   with **no direct shipped equivalent** anywhere surveyed (Radarr/TRaSH
   apply similar thresholds prospectively to new downloads, never
   retrospectively across an existing library).
8. **Growth over time** — items/bytes added per week/month from `first_seen`.
9. **Extract-error / unprobed items audit** — reuses T11's authoritative
   `metadata ? '_extract_error'` GIN-indexed query verbatim; nearly free.
10. **Orphan-sidecar audit** — sidecars whose parent is missing/tombstoned,
    or media missing an expected sidecar. Filearr-specific, falls out of
    T3's sidecar model naturally.
11. **Filtered/custom export** — the general engine (§4) that 1–5 are thin
    presets of; listed last since it *is* the mechanism, not an added report.

Ship **1, 2, 3, 6, 7, 9** first (cheapest + best-validated + the novel one);
**4, 5, 8, 10** fast-follow as thin presets over the same engine.

---

## 9. Schema (DDL)

```sql
CREATE TABLE report_definitions (
    id               UUID PRIMARY KEY DEFAULT uuidv7(),
    name             TEXT NOT NULL,
    owner_principal  TEXT,                          -- mirrors saved_searches R7 placeholder
    saved_search_id  UUID REFERENCES saved_searches(id) ON DELETE SET NULL,
    query            TEXT,                           -- querydsl string; NULL if saved_search_id set
    columns          JSONB NOT NULL DEFAULT '[]',    -- ordered [{key, label?}]
    sort             JSONB NOT NULL DEFAULT '[]',    -- [{key, dir}]
    format           TEXT NOT NULL DEFAULT 'csv'
                     CHECK (format IN ('csv','xlsx','ndjson','json')),
    schedule_cron    TEXT,                           -- nullable; cronsim, T5-tick pattern
    channel_ids      UUID[] NOT NULL DEFAULT '{}',   -- FKs into alert_channels (phase-8)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT report_query_xor_saved_search
        CHECK (num_nonnulls(query, saved_search_id) <= 1)
);
CREATE INDEX ix_report_definitions_owner ON report_definitions(owner_principal);
CREATE INDEX ix_report_definitions_schedule
    ON report_definitions(schedule_cron) WHERE schedule_cron IS NOT NULL;

CREATE TABLE report_runs (
    id                    UUID PRIMARY KEY DEFAULT uuidv7(),
    report_definition_id UUID REFERENCES report_definitions(id) ON DELETE CASCADE,
    canned_report_key     TEXT,        -- registry key; set when report_definition_id is null
    triggered_by          TEXT NOT NULL CHECK (triggered_by IN ('manual','schedule','api')),
    status                TEXT NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued','running','complete','failed')),
    format                TEXT NOT NULL,
    row_count             BIGINT,
    file_size_bytes       BIGINT,
    artifact_path         TEXT,        -- server-local path; never web-root
    error                 TEXT,        -- sanitized at store time (T11 precedent)
    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at           TIMESTAMPTZ,
    expires_at            TIMESTAMPTZ, -- TTL purge target (T5-tick-style periodic task)
    purged_at             TIMESTAMPTZ, -- artifact deleted, row kept for audit trail
    CONSTRAINT report_run_source_xor
        CHECK (num_nonnulls(report_definition_id, canned_report_key) = 1)
);
CREATE INDEX ix_report_runs_definition ON report_runs(report_definition_id);
CREATE INDEX ix_report_runs_expires ON report_runs(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX ix_report_runs_status ON report_runs(status);
```

No `items`/`libraries` changes needed — the low-quality score (§3) is
computed on the fly, never persisted (avoids an invariant-2-violating third
metadata column for a derived value).

---

## 10. Task breakdown — P11-T1..T11

| Task | Size | Goal / Accept | Deps |
|---|---|---|---|
| **P11-T1** | S | `querydsl_to_sqla()` for the existing 8-key grammar. Accept: every filter vector in `shared/querydsl-vectors.json` translates to a SQLAlchemy expression matching a hand-written equivalent (ephemeral-Postgres fixture). | Phase 7 `querydsl.py` |
| **P11-T2** | M | `meta.`/`cf.` grammar extension (co-owned w/ Phase 7) + parameterized JSONB predicate compiler, allow-listed keys. Accept: `cf.rating:>8`/`meta.resolution:1080p` translate to parameterized predicates; a key with SQL metacharacters is rejected at parse time. | P11-T1 |
| **P11-T3** | S | Column-spec resolver + row serializer (core/`m.`/`u.`/bare-effective, per-format coercion). Accept: mixed column list round-trips against a fixture item, matching `effective_metadata` overlay for bare keys. | none |
| **P11-T4** | M | Streaming engine: CSV/NDJSON over `AsyncSession.stream()`+`yield_per`; XLSX `constant_memory`; sync-vs-job threshold routing. Accept: 50k-row fixture export per format at bounded peak memory; over-threshold requests auto-route to a job. | P11-T3 |
| **P11-T5** | S | `report_definitions`/`report_runs` migration + ORM + Procrastinate export task + reconcile sweep (invariant-7-style). Accept: a simulated crash mid-export leaves no row stuck `running` past the next reconcile tick. | §9 DDL |
| **P11-T6** | M | Canned registry (`filearr/reports/canned.py`) for #1,2,3,6,7,9 (§8) + list/run endpoints. Accept: each runs against a seeded fixture DB, returns documented columns. | P11-T1, P11-T4 |
| **P11-T7** | S | Low-quality scorer (`filearr/reports/quality_score.py`), pure function per §3. Accept: worked-example fixtures (sub-720p mpeg4; well-encoded 1080p hevc; 2160p stereo-only-audio) score exactly as §3 predicts. | none |
| **P11-T8** | S | Custom report CRUD, mirrors P3-T7's saved_searches shape; validates query-XOR-saved_search_id + column allow-list; `download`-scope gated. Accept: round-trips; rejects both-set (422). | P11-T2, P11-T3, P3-T7 |
| **P11-T9** | M | Scheduled delivery: same static-tick+cronsim as `scan_cron`; dispatch via `channel_ids` (Phase 8 drivers), attach artifact or signed link by size. Accept: daily-cron report + email channel → exactly one artifact + one delivery/day across a simulated multi-tick test, idempotency key prevents double-fire duplication. | P11-T5, T6/T8, Phase 8 `alert_channels` |
| **P11-T10** | S | RBAC/path-scope integration: compile allowed path prefixes into every report query; require `download`. Accept: a single-subtree-grant principal never sees rows outside it from any report (multi-library fixture). | Phase 6 RBAC (ships pre-RBAC behind scope-only gate; tightens after) |
| **P11-T11** | S | Resource limits + lifecycle: `MAX_ROWS`/`SYNC_MAX_ROWS`/`TTL_HOURS`, dedicated export queue, per-principal cap, TTL purge on T5-tick. Accept: over-`MAX_ROWS` request rejected (422) before query execution; expired artifact deleted by next tick, `purged_at` set, row retained. | P11-T5, T5 tick infra |

Order: **T1 → T3 → T4 → T5** (engine+persistence; parallel-safe with **T7**,
no deps) **→ T6** (earliest user-visible value) **→ T2 → T8** (custom
reports) **→ T9** (scheduling) **→ T10 → T11** (hardening; T10 gated on
Phase 6's own timeline).

---

## 11. Open questions

1. Ship `meta.`/`cf.` grammar (P11-T2) before or after the first canned-report
   wave? Affects whether it gates P11-T6/T8.
2. Scheduled-report email attachment ceiling — no existing setting in Phase 8;
   proposing `FILEARR_REPORT_EMAIL_MAX_BYTES` (~10 MB default) with fallback
   to a signed link above it. Needs a decision, not more research.
3. Canned-report parameterization: light query params (N/library/media_type,
   recommended) vs. full querydsl on canned endpoints.
4. Artifact storage: v1 assumes local server disk, consistent with the
   current no-object-storage stance; revisit once Phase 5 needs multi-node
   artifact access.
5. Should §3's thresholds be per-library/per-profile overridable (TRaSH's own
   anime-vs-standard split is precedent)? Recommend deferring to Phase 4's
   `profiles.py` and shipping one universal table for v1 — affects P11-T7's
   signature, needs confirmation.
6. Duplicate-groups report (§8 item 6) should reuse P3-T10's aggregate once it
   exists; if Phase 11 lands first, P11-T6 needs an interim query with an
   explicit TODO to converge, avoiding two divergent "copy count" paths.
7. Should `report_runs` support cancellation (mirroring `ScanRun`'s SSE
   cancel, T4)? Not a task above; recommend a small P11-T5 addendum if
   confirmed.

---

## Sources cited

- [Tautulli](https://tautulli.com/) · [Exporter Guide](https://github.com/Tautulli/Tautulli/wiki/Exporter-Guide) · [Jellyfin Reports plugin](https://github.com/jellyfin/jellyfin-plugin-reports)
- [Stash Duplicate Checker](https://github.com/stashapp/stash/discussions/5415) · [Stash quality-metrics request](https://github.com/stashapp/stash/issues/2397)
- [beets Smart Playlist](https://beets.readthedocs.io/en/stable/plugins/smartplaylist.html) · [WizTree guide](https://www.diskanalyzer.com/guide) · [TreeSize duplicate search](https://www.jam-software.com/treesize/find-remove-duplicate-files.shtml)
- [Paperless-ngx saved-views PR](https://github.com/paperless-ngx/paperless-ngx/pull/6439) · [export requests](https://github.com/paperless-ngx/paperless-ngx/discussions/6365)
- [TRaSH-Guides Quality Settings (File Size)](https://trash-guides.info/Radarr/Radarr-Quality-Settings-File-Size/) · [Tdarr vs Unmanic 2026](https://www.pistack.xyz/posts/tdarr-vs-unmanic-vs-handbrake-self-hosted-video-transcoding-guide-2026/)
- [XlsxWriter memory/performance](https://xlsxwriter.readthedocs.io/working_with_memory.html) · [SQLAlchemy 2.0 asyncio streaming](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)
- [FastAPI 1M-row CSV streaming](https://medium.com/@connect.hashblock/serving-1m-csv-exports-with-fastapi-and-streaming-responses-without-memory-bloat-32405f42cff5)
- [pyarrow footprint](https://uwekorn.com/2020/09/08/trimming-down-pyarrow-conda-1-of-x.html) · [pandas PDEP-10](https://pandas.pydata.org/pdeps/0010-required-pyarrow-dependency.html) · [apache/arrow#39006](https://github.com/apache/arrow/issues/39006)
- [SQLAlchemy JSONB filtering/injection discussion](https://github.com/sqlalchemy/sqlalchemy/discussions/7991)
- In-repo: `CLAUDE.md`, `docs/execution-plan.md`, `docs/future-roadmap.md`, `docs/phase-1-scanner-tasks.md`,
  `backend/filearr/models.py`, `backend/filearr/tasks/ffprobe.py`, `backend/filearr/querydsl.py`,
  `docs/research/phase-3-search-findability.md`, `docs/tasks/phase-3-search-findability-tasks.md`,
  `docs/tasks/phase-4-data-model-extensions-tasks.md`, `docs/research/phase-6-identity-auth-rbac.md`,
  `docs/research/phase-7-local-query-access.md`, `docs/research/phase-8-alerting.md`,
  `docs/tasks/phase-8-alerting-tasks.md`, `backend/pyproject.toml`.
