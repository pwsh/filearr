# Phase 12 Research Brief: Thumbnail Generation, Storage & Retrieval at Scale

Status: research (Sonnet), 2026-07-07. Companion to docs/research/phase-5-distributed-agents.md
(§4 replication/outbox, reused below). Phase 10 (agent file transfer/retrieval) is concurrently
in-progress; where an agent-side transport is needed, this brief specifies the *contract*
Phase 10 must satisfy rather than assuming its design.

Decision priority (CLAUDE.md / execution-plan.md): **security > integrity > reliability > speed
> compatibility > scalability**. Storage-frugal. AGPL-3.0-compatible OSS only.

---

## 0. Grounding in the codebase

- `config.py`: `config_dir="/config"` is commented **"thumbnails, caches"** — reserved from day
  one. This brief uses `/config/thumbnails/` as the on-disk root.
- `Item.quick_hash` (always computed, xxh3 of first+last 64KiB) and `Item.content_hash`
  (policy-gated per T7) are the existing staleness anchors; `content_hash` can be NULL under
  `quick_only` policy, so the thumbnail cache key must fall back to `quick_hash` — same fallback
  T7 already uses for move-detection.
- `extract.py`/`ffprobe.py` set the house style any new subprocess generator must follow: argv
  list (never shell), hard timeout, output-size cap, failures recorded as an `_..._error` key
  rather than raised (never kill a batch for one hostile file).
- T3's `Item.sidecar_of` FK already links `poster.jpg`/`-thumb.jpg`-style artwork to its parent.
  **Preferring existing artwork over generating anything is this brief's single biggest lever**
  — zero compute, often better quality than an extracted frame.
- T8's `queue_extract`/`queue_index`/`queue_maintenance` + `extract_priority=-10` is the direct
  precedent for giving thumbnails their own low-priority queue.
- phase-5 §4: SQLite `outbox`, per-agent monotonic `seq_no`, idempotent `(agent_id, seq_no)`
  upsert, size-or-age flush (500 rows/5s/2MB). Thumbnails are the first *large* payload this
  design has to carry (12-45KB binary vs KB-scale JSON) — see §6 for why they need a separate
  channel, not the metadata outbox unmodified.

---

## 1. Prior art

**Immich** (closest comparable): 3 derivatives — thumbhash placeholder, small WebP grid, larger
JPEG/WebP preview (1440p default, configurable down to 720p). Maintainers note WebP is smaller
but slower to encode than JPEG; a real regression left superseded JPEGs undeleted after a WebP
migration, doubling disk use — the orphan-artifact failure this brief's GC (§4) must avoid. AVIF
is backlogged, blocked on Flutter client decode support, not server encode cost.
[System Settings | Immich](https://docs.immich.app/administration/system-settings/) ·
[Media Processing | DeepWiki](https://deepwiki.com/immich-app/immich/3.4-media-processing) ·
[immich#22868](https://github.com/immich-app/immich/issues/22868)

**Nextcloud previews** (cautionary tale): a full-scan cron regenerating previews for every file
(not just accessed ones) produced a reported 44GB preview folder against a much smaller source
library, and another install had 11GB of previews for 6GB of source images — the derivative
store exceeded the source. Root causes: no relevance gate, size variants multiplying unchecked,
no orphan GC. Nextcloud's fix was a blunt `preview_max_x/y` ceiling; this brief improves on that
with a fixed small size ladder (§2) plus mandatory orphan GC (§4).
[nextcloud/server#45739](https://github.com/nextcloud/server/issues/45739) ·
[nextcloud/server#5717](https://github.com/nextcloud/server/issues/5717)

**sist2** (already Filearr's own "sist2-style hybrid schema" namesake): stores all thumbnails in
one **LMDB** file keyed by UUID, sidestepping inode pressure entirely. Scales to millions of
images and is version-portable, but LMDB's copy-on-write B+-tree only grows (space reclaim after
deletes is awkward — an open sist2 issue) and has effectively a single-writer path, a poor fit
for many concurrent Procrastinate workers. sist2's own numbers: 8M images at quality=2 ≈
36KB/thumb → 288GB — the cautionary anchor for choosing much smaller per-thumb targets (§2).
[sist2 USAGE.md](https://github.com/sist2app/sist2/blob/master/docs/USAGE.md) ·
[sist2#173](https://github.com/simon987/sist2/issues/173)

**digiKam**: wavelet-compressed BLOBs in a dedicated SQLite DB (`thumbnails-digikam.db`),
typically tens of MB. digiKam's own docs explicitly warn against putting this DB on NFS/SMB —
remote SQLite access degrades badly under latency/concurrency. Since Filearr's `/config` is
meant to be local storage (Postgres already lives there), this validates a local blob-store
approach in general, though this brief still prefers plain files (§1 table).
[digiKam FAQs](https://www.digikam.org/documentation/faq/) ·
[Database Settings](https://docs.digikam.org/en/setup_application/database_settings.html)

**PhotoPrism**: plain filesystem cache (`storage/cache/thumbnails/`) plus a separate sidecar
folder for derived RAW→JPEG conversions — validates filesystem+hash-path layout at real-world
scale, though PhotoPrism also has open reports of unbounded cache growth.
[PhotoPrism Directory Overview](https://docs.photoprism.app/user-guide/backups/folders/)

**Stash**: sprite-sheet + WebVTT scrubbing thumbnails (out of scope, see below), but its explicit
**"Generate Clean"** job — deleting orphaned derivatives whose parent Scene/Image no longer
exists — is exactly the orphan-GC discipline Nextcloud lacks and this brief mandates (§4).
[Stash Media Processing | DeepWiki](https://deepwiki.com/stashapp/stash/4.5-media-processing)

**Jellyfin/Plex trickplay** (BIF sprite grids for seek-bar scrubbing, stored centrally or as
`.bif` sidecars): a materially different feature (hundreds of frames per video, playback-UX
driven) from catalog-grid thumbnails. Filearr has no playback surface (read-only mounts, no
transcoding) — **explicitly out of scope**, note only as a possible v3+ item if playback ships.
[Jellyfin trickplay discussion](https://github.com/jellyfin/jellyfin-meta/discussions/33)

### Storage layout verdict
| Option | Inodes @1M | Backup | Orphan GC | Concurrent writes | Verdict |
|---|---|---|---|---|---|
| Plain files, id-sharded | High | Trivial | Easy | Fine | Workable, inode-heavy |
| Plain files, **content-addressed, git-style 2-level fanout** | Same count, evenly distributed (git-annex: scales to 100M+ files at ≤1024/dir) | Trivial | Easy | Fine | **Recommended** |
| Postgres BYTEA | None | Rides pg_dump/WAL | Easy (FK) | Native | Bloats OLTP tables/backups; against "Postgres = catalog, not blobs" |
| SQLite/LMDB blob sidecar | ~None | Separate backup step | Needs app sweep | LMDB single-writer risk under multi-worker Procrastinate | Viable, real backup cost |

**Recommendation**: `/config/thumbnails/<hash[:2]>/<hash[2:4]>/<hash>-<tier>.webp`, where `hash`
= `content_hash` (fallback `quick_hash`) — **not** a random ID, so the key itself encodes
staleness (§4). Rationale: integrity (correctness = byte-identity, not timestamp comparison);
backup (existing bind-mount story, no second DB to dump); no new runtime dependency; avoids
bloating Postgres OLTP tables/backups/replication (also simplifies phase-5's outbox, which
assumes small JSON payloads — §6).

---

## 2. Formats, sizes, storage budget

**Format: WebP, not AVIF, for now.** AVIF compresses 20-50% better but libaom encodes 5-20x
slower — a 2MB image: <1s WebP vs 10-40s AVIF, ~200MB vs ~2.5GB peak memory. That cost is
incompatible with a batched pipeline whose whole point (per T8) is keeping per-file cost low
across 100k-1M items. SVT-AV1 (~2x faster than libaom) is worth watching but still an order of
magnitude slower than WebP. Immich itself only backlogs AVIF, blocked on client decode, not
server cost. **Recommendation: WebP now; revisit if SVT-AV1 becomes libvips's default AVIF
encoder AND measures <3x WebP cost in a real benchmark.** JPEG rejected outright (no size or
compatibility advantage left in 2026).
[AVIF encoding speed](https://dev.to/serhii_kalyna_730b636889c/avif-encoding-speed-the-numbers-nobody-talks-about-a2h) ·
[WebP vs AVIF 2026 benchmark](https://pixotter.com/blog/webp-vs-avif/)

**Size ladder: two tiers**, one encoder pass, two resizes:
- **grid** — 320px longest edge, quality ~70, target ≤12KB avg.
- **preview** — 800px longest edge, quality ~78, target ≤45KB avg (future detail/lightbox view;
  cheap to generate now, no forced-regeneration later if that view ships).
No blurhash tier in Phase 12 scope (near-zero-cost follow-on, §9 open question).

**Budget math @1M items** (~80% thumbnailable ⇒ 800k): 800,000 × 57KB ≈ **43.6GB** — comparable
to sist2's own per-thumb rate at one size, far below Nextcloud's "previews exceed source"
failure mode. **Recommendation: hard per-file byte caps** (not size-ladder discipline alone):
`thumbnail_grid_max_bytes` (20,000), `thumbnail_preview_max_bytes` (60,000); over-cap at even a
quality floor (50) → store nothing, record `_thumbnail_error`. A soft global-total alarm
(`thumbnail_total_budget_bytes`) surfaces via `/api/system`, not enforced per file.

---

## 3. Generation pipeline per media type

**Rule 0 (highest priority): prefer sidecar artwork over generation.** Check for a T3-linked
image sidecar (`sidecar_of`) before invoking any codec/subprocess; resize that instead. Cheaper
(no ffmpeg/pdfium/trimesh call) and often better quality (curated art vs. an arbitrary frame).

**Images — pyvips (libvips), not Pillow**, for resize/encode. Benchmarks: ~5x faster / ~4x less
memory than Pillow-SIMD on a load→shrink→sharpen→save pipeline; ~3.2x faster specifically for
thumbnailing. libvips is demand-driven/streaming (no full-decode-then-resize), matching the
size-ceiling philosophy already used for docs/3D models — a hostile oversized image can't OOM a
worker. **License: LGPL-2.1-or-later**, compatible as a runtime dependency of an AGPL project (no
relicensing obligation; only modifications to libvips itself stay LGPL). `pillow-simd` rejected:
a fork with a history of lagging upstream Pillow's security patches — would mean tracking a
second image-library security surface. Pillow stays only for the already-wired EXIF read.
[pyvips vs Pillow-SIMD](https://news.ycombinator.com/item?id=45322827) ·
[libvips Speed and memory](https://github.com/libvips/libvips/wiki/Speed-and-memory-use) ·
[libvips LICENSE](https://github.com/libvips/libvips/blob/master/LICENSE)

**Video — ffmpeg single-frame seek**, following `ffprobe.py`'s exact subprocess posture (argv
list, timeout, output cap, tolerant failure). Seek at 10% of duration (already have it from
`metadata_.duration`, no second probe), clamped to a minimum absolute offset (skip black-frame
intros) and below `duration-1s` for short clips. Smart/scene-aware frame selection is assessed
and **rejected as over-engineering for Phase 12** — multiplies ffmpeg cost at 100k-1M scale to
fix a cosmetic (occasional logo-frame), not correctness, problem; revisit only if users complain.

**PDF/documents — pypdfium2, not pdftoppm.** License clean: bindings Apache-2.0/BSD-3-Clause,
bundled PDFium BSD-style — both AGPL-compatible, no copyleft obligation. Rejects `pdftoppm`
(GPL poppler-utils CLI) in favor of an in-process, permissively-licensed binding (no per-file
process spawn, simpler license story). First page only, reuse existing `document_max_bytes`
ceiling. Spreadsheets: **no thumbnail** — no safe cheap "render a spreadsheet" primitive; type
icon placeholder (also avoids adding a new untrusted-input parser for marginal benefit).
[pypdfium2 GitHub](https://github.com/pypdfium2-team/pypdfium2)

**3D models — skip; placeholder icon only.** trimesh's offscreen path (pyrender) needs OSMesa
(software GL) or EGL (GPU context) — a new system dependency across every worker image, to
thumbnail a niche media type, for likely a small fraction of most catalogs. T6 itself only does
"a lightweight file-fact record" (no full geometry parse) for formats without a safe pure loader
— same reasoning applies here. **Recommendation: type-icon now; revisit if 3D libraries prove
significant in real deployments** (none reported in the live test deployment's first scan).
[pyrender offscreen docs](https://pyrender.readthedocs.io/en/latest/examples/offscreen.html)

**Audio/audiobooks — mutagen embedded-art extraction (APIC/equivalent frames).** Cheapest win in
this brief: art bytes are already inside files T6 already opens for tags/chapters; no new
subprocess or dependency (mutagen is already imported via the audiobook extractor). Feed
extracted bytes through the same pyvips resize step. Fallback order: sidecar (rule 0) → embedded
art → placeholder icon; never synthesize art.
[mutagen APIC pattern](https://groups.google.com/g/quod-libet-development/c/cqGVk6RNYkM)

`sample` reuses the audio path (mirrors `EXTRACTORS` mapping already in extract.py). `other`:
placeholder only, no parser trusted.

---

## 4. Staleness detection & GC

**Cache key = content-address, not item ID.** `sha_key = blake2b(f"{hash}:{gen_version}:{tier}")`
where `hash` = `content_hash` (fallback `quick_hash`, mirroring T7). When a file changes, its
hash changes, so the old thumbnail path is simply never looked up again — no invalidation logic,
no mtime-comparison bookkeeping, no race between "scan detected a change" and "cache still
serving old bytes." This applies T7's existing hash-is-identity model to a derived artifact
rather than inventing a new staleness paradigm.

**Scan-integrated trigger**: in `extract_item`, after (re)computing hashes, compare against the
prior stored value; if changed (or never thumbnailed), enqueue a thumbnail job on the new
low-priority queue — deferred after batch commit, per invariant 5, same as extraction jobs
already are.

**Orphan GC (mandatory, not optional — per the Nextcloud postmortem)**: hourly maintenance tick
(T5's tick pattern), diffing a lightweight `thumbnail_manifest` table (§7 DDL) against live
`items.content_hash`/`quick_hash` — cheaper than walking a multi-million-entry directory tree.
Any manifest row with no matching live item/hash/generator-version gets its file `os.remove`d
and its row deleted (plain delete, no tombstone semantics needed — thumbnails are always
regenerable from the still-live source, unlike primary media under invariant 4).

**Generator-version bump = lazy regeneration, not mass invalidation.** Version is baked into the
cache key, so bumping it makes every existing thumbnail simply unaddressed by new requests — no
synchronous mass-regenerate storm across 1M items on deploy. Orphan GC reclaims old-version files
over its normal cadence. Mirrors phase-9's "shadow-rebuild + swap" principle (never mutate a
disposable projection synchronously in place) applied to a different derived store.

---

## 5. Retrieval path

**Immutable, content-addressed static serving**: because the URL *is* the hash, its bytes never
change — serve with `Cache-Control: public, max-age=31536000, immutable` unconditionally (no
ETag round-trip needed at all). `GET /api/items/{id}/thumbnail?size=grid|preview` resolves the
item's current hash server-side and 307-redirects to the immutable path
(`/thumbs/{h[:2]}/{h[2:4]}/{sha_key}.webp`) — clients never need the hashing scheme, and
staleness-triggered regeneration is transparent (next redirect points elsewhere).

**Hybrid pregeneration**: pregenerate **grid** at extract time (cheap, always needed for the
search-results grid, matches the existing batch-commit-then-defer flow). Generate **preview**
on-demand at first request (lazy) — most items in a 1M-item catalog are never opened in detail
view; eagerly generating a second larger derivative for all of them repeats Nextcloud's mistake.
On-miss: synchronous inline for image/PDF (sub-second), queued+polled for video poster frames
(ffmpeg latency variance risks blocking a request under load).

**Queue starvation guard**: dedicated `queue_thumbnail`, low priority (recommend `-20`, lower
than `extract_priority=-10` — a search result can render a placeholder while thumbnailing is
queued; extraction is needed for the row to be searchable at all). Prevents a post-version-bump
regeneration backlog from delaying a concurrently running scan.

**UI placeholder**: type-icon fallback (`onerror`) covers both "will never have one" (skipped
media type) and "doesn't have one yet" (pending job) with one client-side convention.

---

## 6. Agent-side generation (v3)

**Recommendation: agents generate locally; do not defer to central-on-retrieval** — central-
generates-on-retrieval defeats the purpose for files central never receives (v3's "Go agents
with local files central can't reach"). Reuse the identical content-addressing scheme, computed
from the agent's own local hash. Port the pipeline to Go (`bimg`/`govips` — same LGPL libvips C
library, same license posture — plus `ffmpeg` exec, a Go PDF-render binding, a Go ID3/APIC
reader), consistent with phase-5's own "port scanner logic, not code" recommendation for the Go
runtime.

**Transport: not the metadata outbox unmodified.** Phase 5's outbox batching (500 rows/5s/2MB)
assumes KB-scale JSON; a 12-45KB thumbnail is 10-1000x larger and would blow through the 2MB
trigger after a few dozen rows, starving metadata replication behind it in the same
FIFO-by-seq_no drain. **Recommendation**: piggyback on whatever Phase 10 (concurrently in
research) designs as its small-blob transfer-staging mechanism — thumbnails are structurally
identical to any other small-file transfer Phase 10 already needs to solve. This brief specifies
the *contract* Phase 10's channel must satisfy: **no ordering requirement at all** (unlike
metadata rows, thumbnails are independently idempotent by content hash — a real simplification
vs. `seq_no`-ordered replication), retryable, write-if-absent by content-addressed path so a
retried transfer of the same hash is a trivial no-op.

**Bandwidth math**: 100k files on a freshly enrolled agent, 80% thumbnailable, grid tier only
(eager) ⇒ 80,000 × 12KB ≈ **960MB**. At a conservative 5 Mbps uplink, ~26 minutes; on any
LAN/VPN link (phase-5's assumed hub-and-spoke topology), seconds to low minutes. **No special
throttle needed beyond existing outbox-style backpressure**; re-evaluate only if per-agent
catalog size × agent count pushes into tens-of-GB territory.

**Cross-agent dedup**: the cache key excludes `agent_id`, so two agents holding the same file
(by hash) naturally produce one central thumbnail — the second agent's transfer of the same hash
is a no-op on arrival, mirroring `agent_replication_log`'s idempotent-upsert pattern but keyed on
a real content-identity fact rather than just a replay-safety sequence number.

---

## 7. Architecture summary, DDL, config

```
Scan/extract → hash changed? → enqueue queue_thumbnail (low priority)
  → Rule 0: sidecar artwork? yes: resize sidecar; no: per-media-type generator (§3)
  → write /config/thumbnails/{h[:2]}/{h[2:4]}/{sha_key}.webp (grid + preview)
  → upsert thumbnail_manifest row
GET /api/items/{id}/thumbnail?size= → 307 → immutable content-addressed static URL
Hourly maintenance tick → orphan GC (manifest vs. live items anti-join)
v3 agent: identical pipeline in Go → Phase 10 small-blob channel → idempotent by
  (hash, gen_version, size_tier), no ordering required
```

```sql
-- Indexes filesystem-resident thumbnails so GC/staleness checks are cheap Postgres
-- queries instead of million-entry directory walks. NOT the source of truth for
-- thumbnail bytes (the filesystem is) -- itself a disposable, rebuildable projection.
CREATE TABLE thumbnail_manifest (
    sha_key           TEXT PRIMARY KEY,      -- blake2b(hash:gen_version:size_tier), hex
    item_id           UUID NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    hash_used         TEXT NOT NULL,
    hash_kind         TEXT NOT NULL,          -- 'content' | 'quick'
    generator_version INTEGER NOT NULL,
    size_tier         TEXT NOT NULL,          -- 'grid' | 'preview'
    byte_size         INTEGER NOT NULL,
    source            TEXT NOT NULL,          -- 'sidecar' | 'generated' | 'embedded_art'
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_thumbnail_manifest_item ON thumbnail_manifest(item_id);
```
`ondelete="CASCADE"` mirrors T3's sidecar FK reasoning: a hard purge (recycle-bin expiry,
invariant 4) cleans the manifest row automatically; the on-disk file is reclaimed by the next
GC sweep (a sweep, not synchronous delete-on-cascade, so a Postgres transaction never blocks on
filesystem I/O).

```python
# config.py additions
thumbnail_grid_px: int = 320
thumbnail_preview_px: int = 800
thumbnail_grid_quality: int = 70
thumbnail_preview_quality: int = 78
thumbnail_grid_max_bytes: int = 20_000
thumbnail_preview_max_bytes: int = 60_000
thumbnail_generator_version: int = 1
queue_thumbnail: str = "thumbnail"
thumbnail_priority: int = -20
thumbnail_gc_interval_s: int = 3600
ffmpeg_path: str = "ffmpeg"
ffmpeg_timeout_s: float = 30.0
```

---

## 8. Security notes

Generation parses **untrusted, potentially hostile files** — same threat model T1/T3/T6 already
treat as first-class. Apply the identical posture:
- **ffmpeg**: argv list (never `shell=True`), hard timeout, `--` before the untrusted path
  (blocks flag-injection via a leading-`-` filename), exactly as `ffprobe.py` already does.
- **pyvips**: pin a current patched libvips release; cap source dimensions/bytes before the
  resize call (reuse the already-open Pillow EXIF read as a cheap pre-check point), mirroring
  T6's "check size before handing to a parser" discipline.
- **pypdfium2**: reuse the existing `document_max_bytes` ceiling (no new one needed); treat any
  render exception as a soft `_thumbnail_error`, exactly like T6's document extractor.
- **mutagen**: already-trusted code path (T6 already runs it against these files); no new trust
  boundary.
- **No SSRF surface**: no network fetch anywhere in this pipeline — explicitly a non-issue, not
  overlooked.
- **Cache-path traversal**: on-disk paths derive entirely from a locally-computed hash digest,
  never from `filename`/`rel_path`; no code path can produce a `../` segment. `size_tier` is
  never string-concatenated from a request — the retrieval endpoint maps `?size=` through a
  strict `{"grid","preview"}` allowlist before touching a path.
- **Subprocess isolation**: identical to T1's ffprobe precedent, deliberately — no new sandboxing
  primitive introduced or required at this same threat level (untrusted local files).

---

## 9. Task breakdown

Sizes: S = <1 day, M = 1-3 days, L = 3-7 days (matches phase-1 task-sizing convention).

- **P12-T1 (S)** — `thumbnail_manifest` DDL + Alembic migration + config keys (§7). Accept:
  migration applies against ephemeral Postgres (pgserver harness); FK cascade verified with a
  tombstone-purge test. Deps: none.
- **P12-T2 (M)** — Image generator (pyvips): grid+preview encode, byte-cap/quality-floor
  fallback, tolerant errors mirroring `extract_image`. Accept: tests for oversized source
  (rejected pre-check), corrupt image (soft fail), byte-cap fallback, correct output dims/format.
  Deps: P12-T1. Add pyvips/libvips to `backend/pyproject.toml`.
- **P12-T3 (S)** — Sidecar-artwork-first rule: resolve T3 `sidecar_of` image sidecars before any
  generator. Accept: linked `poster.jpg` used verbatim even for a video/audiobook parent. Deps:
  P12-T2.
- **P12-T4 (M)** — Video poster-frame (ffmpeg): 10%-duration seek + clamping, subprocess posture
  per §8. Accept: timeout, missing-binary, short-clip clamp, corrupt-video soft-fail tests. Deps:
  P12-T2.
- **P12-T5 (S)** — PDF first-page (pypdfium2), within `document_max_bytes`. Accept: renders a
  fixture PDF; oversized-source rejection reuses T6's existing fixture pattern. Deps: P12-T2. Add
  pypdfium2 (verify current release license at implementation time).
- **P12-T6 (S)** — Audio/audiobook embedded-art (mutagen APIC). Accept: fixture with/without
  embedded art. Deps: P12-T2 (no new dependency).
- **P12-T7 (S)** — 3D model/spreadsheet/`other` placeholder policy + frontend icon fallback.
  Accept: test asserts no generator ever invoked for these types. Deps: none (parallel to T2-T6).
- **P12-T8 (M)** — Thumbnail task/queue wiring: `queue_thumbnail` dispatch, scan-integrated
  hash-changed trigger (deferred post-commit per invariant 5), writes path + manifest row.
  Accept: fixture-library scan produces thumbnails per media type; re-scan after a content change
  produces a new `sha_key` (old one becomes GC-eligible, not overwritten). Deps: P12-T1..T7.
- **P12-T9 (M)** — Retrieval endpoint: `?size=` allowlist, 307 to immutable static URL,
  `Cache-Control: immutable`. Accept: path-traversal rejection test on a crafted `size=` value;
  cache-header test; hash-fallback (content→quick) redirect test. Deps: P12-T8.
- **P12-T10 (S)** — Lazy preview-tier generation on first request, in-flight dedup guard. Accept:
  grid present after scan, preview absent until first request; concurrent duplicate requests
  don't double-generate. Deps: P12-T9.
- **P12-T11 (M)** — Orphan GC periodic task (T5 tick pattern), manifest-vs-items anti-join,
  reclaimed-bytes logging. Accept: rotated-hash test reclaims old `sha_key`; generator-version
  bump orphans only prior-version rows. Deps: P12-T8.
- **P12-T12 (S)** — Storage budget stats on `/api/system` + soft-alarm log line. Accept: stats
  match `thumbnail_manifest` byte sum. Deps: P12-T1.
- **P12-T13 (L, v3-gated, cross-phase)** — Agent-side generation (Go port of T2/T4/T5/T6),
  transfers via Phase 10's small-blob channel per §6's contract. Accept written against the
  contract (unordered, idempotent-by-hash, retryable) so it's implementable against a stub
  transport and swappable once Phase 10 ships. Deps: Phase 5 (done), **Phase 10 (in progress,
  blocking)**, P12-T1..T8.

**Sequencing**: T1 → {T2 → (T3..T7 parallel)} → T8 → {T9, T11, T12 parallel} → T10 → T13
(blocked on Phase 10). Wave placement (adjacent to Wave 2's data-model work, or Wave 5 for T13
specifically) is an orchestrator call.

---

## 10. Open questions

1. **Blurhash/thumbhash tier**: near-zero-cost follow-on once P12-T2 exists (pixels already
   decoded); decide if it's worth a UX pass over the type-icon fallback.
2. **Where is "preview" actually consumed?** No item-detail/lightbox view exists in the current
   Svelte UI. If none is planned, defer P12-T10 and collapse to a single tier — needs a
   product/architect call, not a research one.
3. **AVIF re-evaluation trigger**: SVT-AV1 as libvips default AND <3x WebP cost — add as a
   tracked recheck item in docs/future-roadmap.md, not silently forgotten.
4. **`thumbnail_manifest` in Postgres vs. filesystem-walk-only GC**: consistent with "Meili
   disposable, Postgres holds persistence" read as "Postgres indexes derived state, filesystem
   holds bytes," but it's a new pattern (first Postgres table solely indexing filesystem-resident
   derived artifacts) — worth an explicit architect ruling.
5. **Phase 10 coupling**: §6's contract is written to be stable regardless of Phase 10's final
   shape, but should be re-checked once Phase 10 lands.
6. **Quality-floor fallback UX**: this brief chose strict budget enforcement (store nothing over
   cap) over storing an over-budget outlier file. A `thumbnail_allow_budget_overage` escape hatch
   could be added cheaply if this bites often in practice.
