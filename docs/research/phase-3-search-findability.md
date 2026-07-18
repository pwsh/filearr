# Research Brief — Roadmap §5: Search & Findability
### "Where did I put that file?" — the P0→P3 feature set

Companion to `docs/future-roadmap.md` §5 (scope) and §8 (Meilisearch feature adoption
plan, referenced not duplicated). Researched 2026-07-07 against the live stack:
FastAPI 0.139 / Python 3.13 / SQLAlchemy 2 + psycopg3 / Postgres 18.4 / Procrastinate
3.9 / meilisearch-python-sdk 7.x / Meilisearch v1.48.3 / Svelte 5 runes + Vite 8 +
Tailwind v4. Constraint ordering for every decision below: **security > integrity >
reliability > speed > compatibility > scalability**. AGPL-3.0-or-later — every new
dependency is license-checked explicitly.

Current baseline (read from the repo, 2026-07-07):
- `backend/filearr/search.py`: flat Meili projection, `SEARCHABLE` = title/filename/
  path/artist/album/author/tags, `FILTERABLE` includes `is_sidecar`/`sidecar_of` (T3),
  `SORTABLE` = title/year/size/mtime. No snippets, no vectors, no hash fields, no geo.
- `backend/filearr/api/search.py`: typed query params → Meili filter string, opaque
  offset-cursor pagination, sidecar exclusion by default. No hash-search endpoint, no
  facet-search endpoint usage, no `/similar`.
- `frontend/src/App.svelte`: single unvirtualized `{#each}` list, 150ms debounce,
  no keyboard nav, no breadcrumbs/copy-path, no highlighting.
- `backend/filearr/models.py`: `Item` has `quick_hash`/`content_hash` (xxh3, T7 policy-
  gated), `metadata_`/`user_metadata` split (invariant 2), `sidecar_of` FK (T3). No
  embedding column, no OCR-text column, no EXIF-specific columns, no saved-search
  table, no tag facet infrastructure beyond the `tags` array.

---

## 1. Findings — Meilisearch capabilities (since v1.48)

*(Full detail lives in roadmap §8; this section only covers what §8 didn't already
capture, plus corrections/updates relevant specifically to §5 features.)*

- **Version:** no confirmed release past v1.48.2/v1.48.3 was found in this pass —
  treat as the current pin being still current, but verify directly against
  `github.com/meilisearch/meilisearch/releases` before the phase 2 build starts, since
  research-agent web coverage of a fast-moving changelog can lag. The two CVEs
  §8 already accounts for (CVE-2026-57823/4, tenant-token auth bypass /
  privilege escalation) are patched at the current pin — no action needed.
  meilisearch.com/docs/changelog
- **Hybrid search:** shipped, not experimental — keyword + vector run in parallel and
  merge via unified scoring (`semanticRatio` tunable). "Composite embedders"
  (different embedder for indexing vs. query time — e.g., a local embedder for query-
  time low latency) remain behind an experimental flag; skip for v1 of this feature,
  revisit once GA. meilisearch.com/docs/capabilities/hybrid_search/overview
- **Hannoy (replaces Arroy):** HNSW-based, LMDB-backed vector index by Clément
  Renault. Experimental ~v1.21, **default since ~v1.29**, and per Meilisearch's own
  March-2026 roadmap post is now the *only* supported vector store (legacy
  `vectorStoreSetting` flag removed, dumpless auto-migration from Arroy). Reported:
  indexing 2 days→2 hours, ~2x smaller on-disk index, ~10x faster search vs Arroy.
  This directly de-risks the P1 "Hannoy backend" line item in scope — it is not a
  future bet, it is already the shipped default at the pinned version.
  blog.kerollmops.com/from-trees-to-graphs-speeding-up-vector-search-10x-with-hannoy
- **Binary quantization:** SIMD bit-collapse; official Meilisearch guidance is **only
  recommended above 1M docs AND 1400+ dimensions** — below that threshold the
  precision loss isn't worth the memory win. **Open gap, flag for direct doc check
  before implementation:** no source in this research confirms BQ has been ported
  from Arroy to Hannoy specifically. Verify `meilisearch.com/docs` for
  `binaryQuantized` support under the current embedder settings schema before
  committing to "quantization from day one" as literally shipped in phase 1 of the
  semantic-search work — it may need to land as a fast-follow once verified.
- **Federated search:** stable since v1.10 (`/multi-search` + `federation`), with
  `facetsByIndex`/`mergeFacets` (v1.11) and remote federated facet search (v1.44).
  Relevant only if Filearr ever splits indexes per media type or per tenant — current
  single-index-per-instance design doesn't need it; note in "open questions" below.
- **Facet search endpoint:** on by default per index, inherits the index's
  `typoTolerance` setting, no separate config — directly enables the P2 tag type-
  ahead item cheaply (§5 P2, "Tag system... Meili facet search endpoint, v1.3+" — now
  confirmed configuration-free).
- **Geo-search:** `_geoRadius`/`_geoBoundingBox`/`_geoPoint` unchanged since 1.48 —
  stable, low-risk to adopt whenever EXIF GPS extraction (P2 below) lands.
- **Recency ranking — the mechanism question the scope calls out explicitly:**
  Meilisearch's default rule cascade is `words → typo → proximity → attribute → sort →
  exactness`. Two distinct mechanisms exist and **must not be conflated**:
  - **Custom ranking rules** (e.g., a static `mtime:desc` rule baked into index
    settings) are always active and are conventionally placed *after* the relevance
    rules, so recency only breaks ties among near-equally-relevant docs.
  - The **query-time `sort` parameter** is literal and only applies when a query
    explicitly requests it; promoting it earlier in the rule order makes ordering
    more literal and *less* relevance-driven.
  - Algolia's "Custom Ranking" (release_date/popularity as canonical recency
    attributes) is the closest prior art and shares Meilisearch's documented pitfall:
    a too-high-precision recency attribute (raw timestamp) can dominate and starve
    every rule after it from ever mattering. **Mitigation, confirmed by Algolia's own
    guidance and applicable directly to Meilisearch: bucket/quantize `mtime` into
    freshness tiers (today / this week / this month / this year / older) rather than
    sorting on the raw epoch integer**, so many documents tie within a tier and later
    rules (or a genuine `sort`) still matter.
  - **Structural gap vs. Elasticsearch/OpenSearch:** `function_score` with
    `gauss`/`exp`/`linear` decay blends recency into the relevance *score itself* as a
    continuous curve. Meilisearch has no equivalent — it can only use recency as a
    discrete tie-breaker via custom ranking rules or an explicit sort. **This is a
    real, permanent Meilisearch limitation for "the file I touched last week beats
    the 2019 copy" as a graded boost** (as opposed to a binary "sort by mtime"). The
    only Meili-native way to approximate a smooth decay is the bucketed-tier
    approach above, tuned to enough buckets that it feels graded.
  - Typesense's `_eval()` bucketed conditional scoring is a closer match to true
    graded decay than anything Meilisearch offers, but a Typesense migration is out
    of scope — noted only so a future re-evaluation of the search engine isn't
    surprised by this gap.
- **Licensing:** core engine confirmed still MIT. A separate BUSL-1.1 Enterprise
  Edition exists but the only confirmed EE-exclusive feature is horizontal sharding —
  hybrid search, tenant tokens, facet search all remain MIT. No BUSL exposure for
  anything in this roadmap item.

**Practical recency design for Filearr:** add a computed, indexed `recency_bucket`
integer (e.g., days-since-mtime bucketed logarithmically: 0/1/7/30/90/365/+) to the
search document at index-build time (computed in `search.py:build_doc`, derived from
`item.mtime`, needs no new Postgres column since it's Meili-projection-only and
disposable/rebuildable — satisfies invariant 1). Add it as a **custom ranking rule**
positioned after `exactness` (default recency-as-tiebreak), and independently expose
raw `mtime` as a **sortable** attribute for users who explicitly want literal
recency-first ordering (a "sort by newest" UI toggle, distinct from the ambient
ranking boost).

---

## 2. Findings — Local embedding models for semantic/hybrid search

CPU-only homelab target (the Proxmox LXC test deployment has no GPU). All models
below are evaluated for **local-only inference** — never cloud, per the security
constraint (embedding private files must never leave the box).

| Model | Params | Dims (MRL) | License | Multilingual | Notes |
|---|---|---|---|---|---|
| **Nomic Embed Text v1.5 / v2-moe** | 137M / MoE | 768→64 (v1.5), 768→256 (v2) | Apache-2.0 | v2: ~100 langs | Frequently cited best size/quality tradeoff for local/edge; v2-moe is first general-purpose MoE embedder |
| **BGE-M3** | 568M | 1024 | MIT | 100+ langs | Still the multilingual reference model; 8192-token context |
| **BGE-small/large-en-v1.5** | 33M / 335M | 384 / 1024 | MIT | English-only | Lightest viable English option |
| **GTE-base/large-en-v1.5, gte-multilingual-base** | — / 305M | Matryoshka-capable, 8192 tok | Apache-2.0 | 70+ langs (multilingual variant) | |
| **Snowflake Arctic Embed 2.0** (m/l) | 113M / 303M | MRL down to 128 bytes compressed | Apache-2.0 | multilingual | No v3 found as of research date |
| **Google EmbeddingGemma** | 308M | MRL 768→128 | — (Gemma license, check terms) | broad | Explicitly on-device targeted, <200MB RAM quantized — strong CPU/homelab fit |
| **Jina embeddings v5-text-nano/small** | 239M | — | **CC-BY-NC 4.0** | broad | High MTEB score but **non-commercial license — excludes it** for a project offering optional paid hosting/support (roadmap §9 license notes) |
| **Qwen3-Embedding** | 0.6B/4B/8B | — | Apache-2.0 (flagged: training-data licensing question raised on GitHub) | broad | Note the flagged ambiguity before adopting |

**Recommendation:** **Nomic Embed v1.5/v2-moe or BGE-M3** — no licensing ambiguity
(Apache-2.0/MIT), proven local-inference track record, multilingual headroom.
**EmbeddingGemma-308M** or **BGE-small (33M/384d)** as a lighter fallback if English-
only is acceptable and CPU budget is tight. Avoid Jina v5 (non-commercial license
incompatible with any future paid-support offering under the AGPL model).

**ONNX Runtime vs. Ollama for the extraction pipeline:**
- ONNX-based serving (sentence-transformers `backend="onnx"`, or Qdrant's fastembed)
  is CPU-first, GPU-optional, and the better fit for a **batch** extraction job (not a
  chat/interactive use case). sentence-transformers documents ~2.5x CPU speedup at
  0.4% accuracy cost switching to ONNX.
- **Ollama is structurally the wrong tool here:** its embedding API was not batch-
  native at launch, and current versions **deliberately serialize embedding
  requests** (traced to a llama.cpp parallel-embedding correctness bug) — this
  directly conflicts with Filearr's Procrastinate worker-pool concurrency model for
  the extract queue. Use ONNX Runtime (via `optimum`/sentence-transformers or
  `fastembed`), not Ollama, for the embedding stage.
- **No authoritative Filearr-representative throughput benchmark exists publicly**
  (nothing for Synology/QNAP/N100/N305-class CPUs). Best available reference points
  (Manticore's ONNX rewrite: 70-230 docs/sec on an unnamed 16-core server for
  all-MiniLM-L12-v2) are not representative of the actual Proxmox LXC hardware.
  **Action: benchmark locally on the test LXC deployment** before committing to a
  default worker concurrency for the embed queue — do not size the rollout plan on
  published numbers.

**Dimensionality vs. binary quantization:** consensus (Meilisearch/Kerollmops,
Qdrant, Weaviate) favors **higher native dimensionality for BQ recall** — Meilisearch
's own guidance is BQ only above 1400 dims; recall@100 at 1024d drops further under
BQ than at 3072d. Countervailing finding (Vespa/HuggingFace): model architecture
matters as much as raw dimension — a smaller, better-trained 384d model can retain
BQ recall better than a larger 768d one. The most directly actionable number found:
mixedbread.ai's MRL+binary combined table shows **512d as a practical "sweet spot"**
(~90.76% retained) vs. 256d (~79.5%) or 128d (~60.3%). **Given Meilisearch's own
1400-dim threshold for recommending BQ at all, and that Filearr's corpus (a homelab
library, not billions of vectors) won't reach the "millions of vectors" scale where
BQ's memory savings are load-bearing, the pragmatic phase-1 choice is: pick a
768-1024d model, defer enabling BQ until item counts actually threaten memory, and
treat "quantize from day one" (as literally stated in scope) as an aspirational
target contingent on the still-open Hannoy-BQ-support verification above** — ship
without BQ first, add it as a fast-follow once (a) verified supported on Hannoy and
(b) corpus size justifies it.

---

## 3. Findings — OCR stack

**License-checked candidates (AGPL-compatible, self-hosted, no cloud):**

| Tool | Code license | Weights license | CPU-viable | Verdict |
|---|---|---|---|---|
| **Tesseract 5.5.2** | Apache-2.0 (confirmed) | n/a (bundled traineddata, same license family) | Yes, CPU-only, ~0.77s/page | **Ship as default engine** |
| **docTR (Mindee) v1.0.1** | Apache-2.0 (confirmed; GPL transitive deps like Unidecode/pyphen removed in v1.0.0 base install) | Apache-2.0 (HF `mindee/` org) | Yes, ~0.12-0.17s/page one benchmark | Good upgrade path; PyTorch now a hard dependency (heavier) |
| **OnnxTR** (community docTR-model wrapper) | Apache-2.0 | same as docTR | Yes, no PyTorch/TF needed | Worth evaluating if dependency weight matters more than doing extra integration work |
| **PaddleOCR v3.7.0 / PaddleOCR-VL** | Apache-2.0 (confirmed, code + base model ERNIE-4.5-0.3B + VL model cards) | Apache-2.0 | Yes, plain CPU wheel available | Stronger accuracy (esp. CJK/degraded docs) but **known 2026 Docker segfault bug** (Paddle issue #76111) on both ARM64/x86_64 — treat as unstable for container deployment right now |
| **RapidOCR** | Apache-2.0 (confirmed) | Inherits PaddleOCR weights — README attributes copyright to Baidu without a fully explicit standalone grant (minor ambiguity, low risk) | Yes, ONNXRuntime CPU by default, lightweight (~27MB wheel) | Good lightweight option; no official Docker image |
| **Granite-Docling-258M (IBM)** | Apache-2.0 (confirmed) | Apache-2.0 | Yes, tiny (258M) | Good CPU/edge VLM-class candidate for later |
| **Surya OCR** | Code: Apache-2.0 (relicensed from GPL-3.0, completed ~2026-05) | **Weights: OpenRAIL-M with revenue/funding/"competitive" restrictions** (>$5M revenue or funding, or competing with Datalab's product, triggers a paid license) | GPU strongly recommended | **Do not adopt** — the weight restriction is a real, explicit compliance risk if Filearr ever crosses the revenue/funding threshold or is deemed competitive with Datalab's commercial API |
| **dots.ocr / dots.mocr** | Conflicting: MIT badge + separate restrictive LICENSE AGREEMENT (anti-bulk-scan clause, PRC arbitration, forced migration) + hard-pins AGPL PyMuPDF | n/a | — | **Avoid entirely** — directly conflicts with a bulk file-scanning app's normal operation |
| **Nanonets-OCR-s** | Mislabeled Apache-2.0 on HF; actually non-commercial Qwen Research License (maintainer-confirmed) | non-commercial | — | **Avoid** — license badge is wrong |

**Recommendation:** **Tesseract 5.5.2 (Apache-2.0) as the default/always-on OCR
engine**, matching its CPU-only cost profile and zero licensing risk. Treat
**PaddleOCR** as a P2+ "better accuracy" opt-in once its Docker stability bug is
resolved upstream (re-check `github.com/PaddlePaddle/Paddle/issues/76111` before
adopting). Explicitly **exclude Surya, dots.ocr, and Nanonets-OCR-s** from the
dependency list with the specific reasons above documented in code comments (this
class of "looks-open-but-isn't" license trap is exactly the kind of regression
CLAUDE.md's gotchas section is meant to prevent — add a line there once shipped,
mirroring the existing guessit/tinyMediaManager/FileBot notes).

**Caching strategy (this is where the "Meili is disposable" invariant bites hardest
for OCR — OCR is expensive, so the *text* must be cached in Postgres, never only in
the disposable index):**
- **Recoll precedent (closest match):** hash-keyed OCR-output cache with a path-based
  fast path — "OCR is only performed if the file was not previously processed or if
  it changed," hash recomputed only when the file moved. This is the model to copy.
- **Paperless-ngx precedent:** MD5 checksum used only for *ingest dedup* (reject
  re-uploads), not an OCR-result cache; extracted text lands directly in a DB text
  column (`Document.content`), not a sidecar file. Re-OCR is a manual "Redo OCR"
  action, not automatic.
- **Docspell precedent:** SHA-256 hash on upload; identical-hash files are dropped
  from the processing set before OCR ever runs — the most direct "content-hash
  avoids repeat work" example, though it's ingest-level, not a standing cache.
- **Filearr design (synthesizing all three, fitting the existing schema):** Filearr
  already computes `quick_hash`/`content_hash` per T7's policy. Store OCR output text
  in `metadata_` (extracted, invariant 2 — never `user_metadata`) under a key like
  `metadata_.ocr_text`, alongside `metadata_.ocr_hash` recording the hash of the
  bytes that were OCR'd. On a rescan, **skip OCR if `ocr_hash` still matches the
  current content/quick hash** (honoring T7's hash policy — on `quick_only`
  libraries, gate on `quick_hash`; accept the same ambiguity tradeoff T7 already
  accepts for move detection). This makes the OCR cache fully Postgres-resident and
  rebuildable (satisfies invariant 1 — if Meili is wiped, `rebuild_index` re-reads
  `metadata_.ocr_text` with zero re-OCR cost).

**OCR trigger policy (cross-tool synthesis):**
- Universal pattern across ocrmypdf/Paperless/Docspell: **attempt cheap native text
  extraction first** (pypdf/docx already extracts properties per T6 — extend to body
  text), **gate OCR behind a "extracted text below N characters" threshold**
  (Paperless uses 50 chars, Docspell uses 500 — no universal standard, pick one and
  document it, e.g. `FILEARR_OCR_MIN_TEXT_CHARS`, default 100).
  - **Recoll is the only tool with per-directory/per-library OCR opt-in** — direct
    precedent for Filearr's per-library model. Paperless/Docspell keep OCR policy
    global despite having other per-folder routing. **Adopt Recoll's model**: add a
    per-library `ocr_enabled` toggle (mirrors the existing `enabled_types` /
    `hash_policy` per-library override pattern already in `models.py`).
  - Two independently-tunable ceilings recur everywhere: **page/image pixel cap**
    (ocrmypdf `--skip-big`, Docspell 14MP, Paperless `OCR_MAX_IMAGE_PIXELS`) and
    **page-count/time cap** (Docspell first-10-pages, Paperless `OCR_PAGES`,
    ocrmypdf 180s/page timeout). Mirror both as `FILEARR_OCR_MAX_PIXELS` and
    `FILEARR_OCR_MAX_PAGES`/`FILEARR_OCR_TIMEOUT_S`, following the existing
    `FILEARR_MODEL3D_MAX_BYTES`/`FILEARR_DOCUMENT_MAX_BYTES` size-ceiling convention
    from T6.
  - All PDF-touching tools check for an existing text layer before OCRing — this
    validates "skip OCR if text layer exists" as the default policy Filearr should
    also use for image-based-vs-text-based PDF pages.

---

## 4. Findings — Duplicate-detection UX

**Prior art surveyed (czkawka, dupeGuru, Immich, plus a broad market scan of Google
Photos/Syncthing/digiKam/PhotoPrism/Synology/Nextcloud/Bynder/Cloudinary): every
single tool treats duplicate awareness as a heavyweight, separate-screen workflow.
None ship a lightweight inline "N copies exist" badge in a primary search-results
list.**

- **czkawka/Krokiet:** flat list with synthetic header rows simulating groups (not a
  real tree); smart-select rules (all-except-oldest/newest); dedicated Compare window
  for image duplicates; hardlink/symlink as a first-class action alongside delete.
- **dupeGuru:** most fully-developed pattern — one **structurally-protected reference
  file** per group (its delete-checkbox is disabled entirely — "a security measure to
  prevent dupeGuru from deleting not only duplicate files, but their reference"),
  duplicates indented below it; **Selected vs. Marked** is a genuinely useful
  decoupling (browsing/inspecting vs. committing to a destructive action); a
  Deletion Options dialog offers link-instead-of-delete before any destructive
  commit.
- **Immich:** two separate mechanisms — (1) exact-checksum dedup on upload, but
  scoped **per-library only** (not global across libraries — same gap Filearr would
  have without deliberate design), and (2) CLIP-embedding-based near-duplicate
  detection surfaced ONLY in a dedicated `/utilities/duplicates` review page,
  populated by an async job, never inline in the timeline/search view. A GitHub
  discussion explicitly criticizes this dedicated-workflow UX as clunky — validating
  that Filearr's planned lightweight-badge approach is a genuine differentiator, not
  a lesser imitation of an existing solved pattern.
- Universal secondary pattern worth borrowing regardless of UX surface:
  **soft/deferred destructive actions** (trash-then-purge, not immediate delete) —
  this already matches Filearr's own invariant 4 (scans never hard-delete;
  tombstone + recycle-bin purge), so the duplicate-resolution flow should reuse the
  exact same `trashed` status/recycle-bin mechanism rather than inventing a second
  deletion path.

**Filearr design implication:** because exact-duplicate detection is comparatively
cheap here (Filearr already computes `content_hash`/`quick_hash` per-item — no CLIP/
ML pipeline required, unlike Immich's near-duplicate approach), the badge is
low-cost to build and is legitimately unaddressed by any competitor. Recommended
UX: a small "N copies" pill on a search-result row (rendered from a
Meilisearch facet/count on `content_hash`, computed at query time — see Schema
section), clicking it expands an inline (not full-page-navigation) list of the
other copies with path + library + last-seen, each with the standard "open
containing folder / copy path" row actions already planned for P0. No separate
dedupe tool needed for v1; a dupeGuru-style dedicated resolution view (bulk
hardlink/trash-extras) can be a later addition once the badge affordance proves
useful in practice.

---

## 5. Findings — EXIF/GPS extraction

- **Tool choice, validated against what comparable OSS projects actually ship:**
  Immich uses `exiftool-vendored` (wraps the real Perl exiftool binary with
  `-stay_open` process pooling). PhotoPrism uses a native Go EXIF parser as primary
  with exiftool as an explicit gap-filler (`PHOTOPRISM_EXIFTOOL_BIN` config,
  documented as a required install-time dependency in practice). digiKam hard-
  requires Exiv2 as its core library but *also* falls back to exiftool for cases
  Exiv2 handles poorly (notably DNG/RAW writes). **Every mature comparable project
  keeps real exiftool in the pipeline in some capacity** — a de facto industry
  acknowledgment that no substitute fully replicates its continuously-updated tag
  database.
- **Recommendation: exiftool via subprocess with `-stay_open` batch/daemon mode**
  (matches Immich's approach exactly), avoiding the per-file process-startup cost
  that would otherwise be prohibitive for a scan touching thousands of files.
  - License: dual Perl Artistic License / GPL ("same terms as Perl itself") —
    actively maintained (v13.59, 2026-05-27).
  - **PyExifTool is stale** (canonical fork `sylikc/pyexiftool` last released
    2023-10-22, no commits found through mid-2026) — do not add it as a dependency
    as-is. Either vendor a small (~200-line) custom `-stay_open` pipe-protocol
    wrapper, or re-evaluate PyExifTool's freshness immediately before implementation
    in case it has since resumed activity.
  - **Avoid exiv2 Python bindings** (py3exiv2/pyexiv2/python-exiv2): all GPL-2.0-or-
    later or GPL-3.0-or-later, and — this is the one to get right — **in-process
    linking via Python bindings is the FSF's own example of forming a single
    combined GPL work** (per the GPL FAQ's "communication mechanism" test: simple
    subprocess/exec/pipe communication reads as "separate programs," but intimate
    in-process linking does not). Exiv2's own docs additionally concede exiftool has
    superior maker-note/format coverage. Net: no reason to take on either the
    licensing ambiguity or the weaker coverage — **subprocess-based exiftool only**,
    consistent with how Filearr should treat exiftool itself (external binary, never
    linked in-process).
  - Pure-Python alternatives (`exifread`, `piexif`) don't approach exiftool's
    breadth; `piexif` is effectively abandoned (last release 2019, unpatched Snyk-
    tracked vulnerability) — do not use for new work.
  - **Video/audio bonus finding relevant to T1/T6 extractors:** exiftool reads
    **timed GPS metadata streams** from MOV/MP4 containers (dashcam/drone/smartphone
    location tracks, e.g. `QuickTime:GPSCoordinates`, Apple's
    `com.apple.quicktime.location.accuracy.horizontal`) that neither ffprobe nor
    mutagen can extract — this is a real, sourced gap in the current T1 ffprobe-only
    video pipeline. Recommend running the same exiftool `-stay_open` pool as a
    narrowly-scoped supplementary extractor for GPS/device/timestamp fields on
    video files, not a wholesale replacement of ffprobe (which remains authoritative
    for codec/stream technical facts).
- **GPS privacy — this is a security-tier decision, not a nice-to-have:**
  - **No mainstream self-hosted photo/file tool has a mature, well-enforced "don't
    expose GPS externally by default" control.** Immich shows maps/GPS on by default
    and had a documented shared-link EXIF/GPS leak gap (multiple GitHub discussions
    requesting stripping; a "Share metadata" toggle exists but enforcement has had
    gaps). PhotoPrism stores and indexes GPS regardless of its `DISABLE_PLACES` env
    var (which only suppresses UI/lookup, not storage/API exposure). **This is a
    documented gap across the entire category, not a solved problem to copy** —
    Filearr shipping a genuinely safe default is a real differentiator, not table
    stakes to merely match.
  - **Formal vulnerability classification exists for this exact mistake:** CWE-1230
    "Exposure of Sensitive Information Through Metadata." Real CVEs confirm it's
    treated as a bona fide vulnerability class, not a preference: CVE-2023-1974
    (Answer Q&A platform failed to strip EXIF/GPS before serving uploaded images,
    CVSS 6.5), CVE-2026-27892 (FacturaScripts, same class of failure).
  - **Real-world justification:** John McAfee's Dec 2012 Guatemala location was
    confirmed via EXIF GPS in a published photo (NPR, PetaPixel, Scientific
    American all confirm). Academic precedent: Friedland & Sommer's "Cybercasing the
    Joint" (USENIX HotSec 2010) found ~1.3% of a 68,729-image Craigslist sample
    carried GPS EXIF, some depicting valuables at identifiable home addresses.
    (Note: a commonly-repeated "60% of Craigslist photos" figure has **no supporting
    source** — do not cite it; the real, sourced figure is 1.3%.)
  - **mat2 (Metadata Anonymisation Toolkit 2)** is the strongest direct precedent for
    "strip location metadata before external exposure as an accepted default" —
    bundled by default in Tails OS and Qubes-Whonix specifically for this purpose.
  - **Design decision for Filearr:** GPS fields extracted by exiftool land in
    `metadata_` (extracted, per invariant 2) as normal, but **default to excluded
    from the Meilisearch projection and from public API responses** (not returned in
    `effective_metadata` API payloads by default; not added to `FILTERABLE`/geo-
    enabled in `search.py`) **unless explicitly opted in per-library** (a new
    `Library.expose_gps` boolean, mirroring the existing per-library toggle
    pattern — `hash_policy`, `watch_mode`, `enabled_types`). This is the CWE-1230-
    avoidance default and should be documented in CLAUDE.md's invariants once
    implemented, since it's exactly the kind of privacy-by-default decision that's
    easy to accidentally regress later (e.g., a well-meaning contributor adding GPS
    to `FILTERABLE` for the geo-map feature without realizing the opt-in gate).

---

## 6. Findings — Frontend: virtualization + keyboard UX

**Virtualization for Svelte 5 runes (current frontend is unvirtualized `{#each}`,
`frontend/src/App.svelte`):**

| Library | Svelte 5 support | Maintenance (as of research date) | Verdict |
|---|---|---|---|
| **virtua (`virtua/svelte`)** | Hard `svelte: ">=5.0"` peerDependency, runes+snippets native | Active — v0.49.2 released 2026-06-29, steady 2-4 week cadence through 2026, 3.6k stars | **Primary recommendation** |
| **@humanspeak/svelte-virtual-list** | Svelte-5-only by design (README states outright) | Very active — v0.5.9 published 2026-07-03 (days before this research), 54 releases | **Strong alternative**, smaller community (94 stars) |
| **@tanstack/svelte-virtual** | Permissive `svelte: "^3\|\|^4\|\|^5"` peer range, NOT runes-native | Confirmed **open, ~21-month-old bug**: GitHub issue #866 (opened 2024-10-28, still open) — empty list/table under Svelte 5, requires manual `$virtualizer._willUpdate()` workaround. Broader TanStack-org pattern of slow Svelte-adapter investment (TanStack Query has similar year-plus-stale Svelte 5 PRs) | **Avoid** |
| **sveltejs/svelte-virtual-list (official)** | Svelte 2/3-era `let:item` slots, no runes | **Abandoned** — last publish 2021-06-22 | **Avoid** |
| **svelte-tiny-virtual-list** | Works via snippet syntax but not internally rune-rewritten | Slowing — last real publish 2025-07-05, disproportionate open issue/PR backlog | **Caution, not first choice** |

**Recommendation: `virtua/svelte`** for the primary implementation — largest
community, enforced Svelte-5-only dependency (no legacy-compat risk), and mature
dynamic-item-sizing support needed once result rows show thumbnails/snippets of
varying height.

**Search-results-specific virtualization concerns (design notes, not library
features):**
- **Variable row height** (once P1 snippets/thumbnails land): measure rows post-
  paint via ResizeObserver, cache by stable key (item id), reserve space via
  `min-height`/`aspect-ratio` placeholders for async thumbnail loads to prevent
  scroll-jump. virtua supports this natively.
- **Scroll position restoration** on return from a detail view: persist
  `{scrollOffset, selectedIndex}` keyed by the current query (Svelte 5 `$state` or
  `sessionStorage`), feed back into the virtualizer's initial-offset prop on
  remount. (No router currently in the frontend — App.svelte is a single-page
  tab-switch model — so this is a lightweight store, not a router-integration
  problem, for now.)
- **Keyboard nav + virtualization — the critical architectural point:** when a
  focused row scrolls out of the virtual window, its DOM node unmounts and native
  browser `:focus` is lost. **Do not drive keyboard selection via real DOM focus.**
  Use a **roving logical index** (`selectedIndex` in state) + `aria-activedescendant`
  on a stable container for visual highlighting/accessibility, and call the
  virtualizer's imperative scroll-into-view API on every index change. This is the
  VS Code / GitHub command-palette pattern and is the only approach that survives
  virtualization correctly.

**Keyboard-first UX prior art (Everything, Alfred, Raycast) — translated to a web
SPA:**
- **Focus shortcut:** `/` as primary (Everything/vim convention, no OS/browser
  collision); Cmd/Ctrl+K as an optional alias with an `activeElement` guard and
  careful `preventDefault()` (browsers own Ctrl/Cmd+K in some contexts — test per-
  browser).
- **Instant filtering:** Everything's synchronous local-index model is the aspirational
  UX target, but Filearr's Meilisearch-over-network model needs debounce (~100-
  150ms, matching the current 150ms already in `App.svelte`) plus
  `AbortController`-based in-flight request cancellation to avoid out-of-order
  response races that Everything, as a local-index tool, never has to solve.
- **Arrow keys + Enter + modifier+Enter** — convergent pattern across all three
  tools: Enter = open/primary action, Ctrl/Cmd+Enter = secondary action (map to
  "copy resolved path" — respecting `library.native_prefix`, per invariant 3 — or
  "reveal in containing folder"), Shift+Enter = open in new tab (web-native, not
  from the reference tools).
  - `Ctrl/Cmd+Enter = copy path` and `open containing folder` are two different
    P0 actions per scope — recommend Cmd+Enter for copy-path (matches Alfred's
    Cmd+Return=reveal-in-Finder muscle memory closely enough) and a dedicated
    row-level icon/button for "open containing folder" (Filearr is a web app; "open
    containing folder" needs either a `file://` link — browser-security-restricted
    — or a native-agent handoff, so treat it as a lower-certainty P0 item pending a
    decision on mechanism; see Open Questions).
- **Preview:** Alfred's Shift/Cmd+Y Quick Look → map to **Space** opening an inline
  preview panel when the result list (not the search input) has focus.
- **Action panel on a result:** Raycast's Cmd+K contextual action menu (auto-
  assigning Enter/Cmd+Enter to the first two actions rather than hand-wiring each
  view) is a strong fit for Filearr's existing PATCH/batch metadata-edit API and
  should gate visible actions by the caller's API-key scope (read/write/admin, per
  `security.py`).
- **Escape:** universal layered step-back (clear query → close preview → close
  action panel).
- **Web-platform constraints not present in the three native reference tools:**
  Ctrl/Cmd+K and Ctrl/Cmd+F are browser-owned in some contexts and require
  `preventDefault()` (must test cross-browser; some combos like Ctrl+W can never be
  intercepted). A web SPA shares the page with other focusable elements — global
  shortcuts must check `document.activeElement` before firing, unlike an OS-level
  overlay. Screen-reader parity (`role="listbox"`, `aria-activedescendant`,
  `aria-live` result-count announcements) has no equivalent in the native reference
  tools (they lean on OS accessibility APIs) and must be designed from scratch.
- **Inline filter syntax:** Everything's `ext:`/`size:`/`dm:`/type-macro vocabulary
  maps almost directly onto Filearr's existing facets. Recommend a small v1 subset —
  `ext:mp4`, `type:video`, `size:>1gb`, `lib:<name>` — parsed **client-side** into
  the existing `build_filters()` query-param shape (or a small client-side→
  query-param translator hitting the same `/search` endpoint), deferring full regex
  mode since Meilisearch's typo-tolerant engine isn't regex-native.

---

## 7. Phased architecture (P0 → P3)

Each phase is additive; Meili documents/settings remain fully derivable from
Postgres at every step (invariant 1). No phase requires a schema migration that
can't be re-run from scratch via `rebuild_index`.

### P0 — Core payoff (builds directly on current `search.py`/`api/search.py`/`App.svelte`)
- Path breadcrumbs + copy-path action in the results list (pure frontend + existing
  `path`/`rel_path`/`library.native_prefix` data — no backend change).
- "Open containing folder" — **needs an open-question resolution** (see below); do
  not block the rest of P0 on it.
- Hash search: expose `quick_hash`/`content_hash` (already stored, T7) as
  **filterable, typo-tolerance-disabled** Meili attributes; add a `hash=` query
  param to `/search` for exact lookup. MD5/SHA-256 on-demand: add a lazy per-item
  endpoint (`GET /items/{id}/hash?algo=sha256`) that streams-hashes on request and
  caches the result in `metadata_` (extracted fact, invariant 2) — never computed
  eagerly on every scanned file (would regress T7's network-mount cost discipline).
- Quick filter chips (kind/size/date) + facetStats-driven range sliders — thin
  frontend layer over the facet distribution already returned by `/search`.
  `facetStats` needs a `numeric` facet backing (size, mtime) — confirm Meili's
  facet-stats support for numeric ranges at the pinned version before implementation
  (should already be present at 1.48.3; low risk).
- Recency ranking: add computed `recency_bucket` to `search.py:build_doc`
  (Meili-only, disposable) as a post-exactness custom ranking rule; keep `mtime` as
  a separate explicit-sort option. (Detailed design in Finding §1 above.)
- Frontend: adopt `virtua/svelte` for the results list; implement roving-index
  keyboard nav (arrow keys, Enter, Cmd+Enter) with `aria-activedescendant`; `/` to
  focus search.

### P1
- Content extraction: extend T6's document/spreadsheet property extractors with a
  **separate, independently-bounded body-text pass** (pypdf/python-docx text,
  already flagged as deferred-to-v2 in roadmap §15) — same zip-bomb/decompression-
  ratio guard discipline called out there. Store extracted text in
  `metadata_.body_text` (invariant 2), feed into a new Meili searchable attribute
  with `attributesToHighlight`/`crop` for snippets.
- OCR (Tesseract 5.5.2 via `-stay_open` subprocess pool): trigger policy = extracted-
  text-below-threshold (`FILEARR_OCR_MIN_TEXT_CHARS`), per-library `ocr_enabled`
  toggle (Recoll precedent), hash-gated cache in `metadata_.ocr_text` +
  `metadata_.ocr_hash` (Recoll/Docspell-synthesized design, §3 above). Page/pixel/
  timeout ceilings via `FILEARR_OCR_MAX_PAGES`/`FILEARR_OCR_MAX_PIXELS`/
  `FILEARR_OCR_TIMEOUT_S`, matching the T6 size-ceiling convention.
- Saved searches / smart folders: new `saved_searches` table (id, name, owner/
  api-key-scope reference, query params as JSONB, created_at) — a thin persisted
  wrapper around the existing typed `/search` query params, ACL-aware once RBAC
  (roadmap §3) lands. Not a Meili concept at all — pure Postgres, trivially
  rebuildable-compatible since it never touches the index.
- Semantic/hybrid search: local ONNX-served embedder (Nomic v1.5/v2-moe or BGE-M3,
  §2 above) running as a new extract-pipeline stage (batch job, Procrastinate task,
  deferred-after-commit per invariant 5); embeddings stored **only** in Meili
  (disposable, invariant 1 — do NOT persist raw vectors in Postgres, since Meili can
  regenerate them via `rebuild_index` re-running the same embedder against
  `metadata_.body_text`/title/tags — but DO persist the *embedder model identifier
  + version* in a settings/config table so a `rebuild_index` run knows which model
  produced the currently-configured vectors and can detect drift). Defer binary
  quantization until (a) Hannoy BQ support is directly verified and (b) real corpus
  size justifies it (§2 above) — ship dense vectors first.
- `/similar` endpoint: thin wrapper once embeddings exist — Meili-native, no new
  Postgres schema.
- Duplicate-awareness badge: "N copies" pill computed from `content_hash`/
  `quick_hash` group counts (facet count at query time, or a lightweight Postgres
  aggregate refreshed alongside the search projection), inline expansion (not page
  navigation) showing the other copies. No ML pipeline needed (§4 above).

### P2
- Timeline browsing: date histogram over `mtime`/`first_seen`, thin frontend
  navigation layer over existing sortable/filterable date fields.
- EXIF deep extraction: exiftool `-stay_open` pool (§5 above) as a new extractor
  stage alongside T1/T6, writing into `metadata_` (camera/lens/exposure/dimensions
  fields) plus the video-GPS-track supplemental pass. **GPS fields default-excluded
  from Meili projection and public API** behind a new per-library `expose_gps`
  toggle (§5 above) — implement the opt-in gate in the *same commit* that adds GPS
  extraction, never as a follow-up (regression risk otherwise).
- Tag facet type-ahead: Meili facet-search endpoint against the existing `tags`
  array — no new backend infra needed beyond wiring a `/tags/search` proxy endpoint
  (confirmed configuration-free per §1 above).
- Archive/email indexing: zip/7z member listing (bounded by the same decompression-
  ratio-guard discipline as T6's zip-bomb concern), mbox/PST deferred further (higher
  complexity, lower payoff — Recoll precedent treats this as a late add too).
- NL query assist: query→filter translation; **local LLM only** (never cloud, same
  constraint as embeddings/OCR) — treat as an optional, clearly-labeled experimental
  feature; do not block core search on it.

### P3
- File provenance: source URL / originating-agent columns — depends on roadmap §7's
  extended-metadata/provenance columns and §1's distributed-agent work; track as a
  cross-cutting item, not solely owned by §5.
- Frecency ranking profiles: per-user personal ranking — depends on roadmap §3's
  auth/identity work landing first (frecency needs a stable user identity to
  attribute access history to); explicitly sequenced after RBAC.

---

## 8. Schema changes (summary)

- `libraries.ocr_enabled` (bool, default false or auto per size/type heuristic —
  decide at implementation time) — P1.
- `libraries.expose_gps` (bool, default **false**) — P2, ships together with EXIF
  GPS extraction, never separately.
- `saved_searches` table (id uuidv7, name, api_key_id or owner ref, query jsonb,
  created_at) — P1.
- No new columns needed for hash search (P0) — `quick_hash`/`content_hash` already
  exist.
- No raw embedding-vector Postgres column — vectors live only in Meili (invariant
  1); add a small `search_config`/settings row (or extend existing config table)
  recording `embedder_model_id` + `embedder_model_version` so `rebuild_index` can
  detect a model change and know a full re-embed is required.
- `metadata_` (JSONB, no migration needed) gains new well-known keys over time:
  `ocr_text`, `ocr_hash`, `body_text`, EXIF fields (camera/lens/gps/exposure), video
  GPS-track fields — all extracted facts, all invariant-2-compliant, all covered by
  the existing GIN index on `metadata`.

## 9. Config surface (new `FILEARR_*` env vars)

- `FILEARR_OCR_ENABLED` (global default), `FILEARR_OCR_MIN_TEXT_CHARS` (default
  ~100), `FILEARR_OCR_MAX_PAGES`, `FILEARR_OCR_MAX_PIXELS`, `FILEARR_OCR_TIMEOUT_S`.
- `FILEARR_EMBEDDER_MODEL` (e.g. `nomic-embed-text-v1.5`), `FILEARR_EMBEDDER_BACKEND`
  (onnx, default), `FILEARR_EMBEDDER_DIM`.
- `FILEARR_EXIFTOOL_PATH` (subprocess binary location, matching the existing
  `ffprobe`/external-binary pattern from T1).
- `FILEARR_GPS_EXPOSE_DEFAULT` (global default for the new per-library
  `expose_gps` toggle — global default should be **false**).

## 10. Security notes (summary, expanding on findings above)

- **OCR and embedding inference must run locally, never call a cloud API** — Ollama
  is excluded for its serialization/throughput issue, not for a security reason, but
  the ONNX-local recommendation also happens to satisfy the no-cloud constraint by
  construction (no network egress in the model-serving path).
- **GPS defaults to hidden** (public API + Meili projection) unless explicitly
  opted in per-library — justified by CWE-1230, real CVEs in the same class,
  and the McAfee/Cybercasing precedents (§5). This is the single highest-priority
  security decision in this brief; implement the gate in the same change that adds
  GPS extraction.
- **exiftool/Tesseract invoked as subprocesses only**, never as in-process linked
  libraries — avoids the exiv2 GPL-2.0 in-process-linking ambiguity entirely and
  matches the existing ffprobe subprocess pattern (T1) and the untrusted-input
  posture already established for NFO parsing (T3, defusedxml/XXE-safe).
- **License-excluded dependencies, explicit list for CLAUDE.md's gotchas section
  once implemented:** Surya OCR (OpenRAIL-M weight restrictions), dots.ocr/
  dots.mocr (conflicting license + anti-bulk-scan clause), Nanonets-OCR-s
  (mislabeled non-commercial), Jina embeddings v5 (CC-BY-NC), PyExifTool
  (unmaintained — not a license problem, a freshness problem, but note it
  alongside the license list since it drove the same "look before adopting"
  decision), exiv2 Python bindings (GPL-2.0 in-process linking ambiguity).
- Zip-bomb/decompression-ratio discipline (already deferred from T6 as roadmap §15)
  must extend to any new archive-member indexing (P2) and to OCR'd/embedded PDF
  content — apply the same size-ceiling-before-parse pattern already established.

## 11. Task breakdown (T-numbered, continuing from phase-1's T1-T19 numbering scheme
used elsewhere in `docs/`)

| # | Task | Size | Accept criteria |
|---|---|---|---|
| T20 | Hash search: filterable/typo-off `quick_hash`/`content_hash` in Meili, `hash=` query param, on-demand MD5/SHA-256 endpoint with `metadata_` caching | S | Exact hash lookup returns correct item(s); on-demand hash computed once and cached, not recomputed on repeat calls |
| T21 | Recency ranking bucket + custom ranking rule + explicit sort toggle | S | Two nearly-identical-relevance docs with different mtimes order by recency; explicit "sort newest" still available and literal |
| T22 | Frontend: adopt virtua/svelte, roving-index keyboard nav, `/` focus shortcut, breadcrumbs + copy-path row actions | M | 100k-item mock dataset scrolls smoothly; arrow keys keep selection visible and announce via aria-activedescendant; copy-path respects `native_prefix` |
| T23 | Quick filter chips + facetStats range sliders (kind/size/date) | S | Chips reflect live facet counts; range slider bounds derived from facetStats, not hardcoded |
| T24 | Body-text extraction (pypdf/docx) with decompression-ratio guard, feeding Meili snippets/highlighting | M | Search hit shows a relevant highlighted snippet; a crafted zip-bomb-style docx/xlsx is rejected before full parse, not after |
| T25 | OCR pipeline: Tesseract `-stay_open` pool, trigger-threshold policy, per-library toggle, hash-gated cache in `metadata_` | L | Re-scanning an unchanged scanned PDF does not re-invoke Tesseract; OCR text appears in search; per-library toggle actually gates the extractor |
| T26 | Saved searches table + API (CRUD, ACL-ready) | S | Saved query round-trips to the same results as re-running the original params |
| T27 | Local embedder integration (ONNX, Nomic/BGE-M3), hybrid search wiring, embedder-version tracking for rebuild-drift detection | L | `rebuild_index` regenerates identical vectors from Postgres alone; changing `FILEARR_EMBEDDER_MODEL` is detected and flagged, not silently mixed with old vectors |
| T28 | `/similar` endpoint | S | Returns plausible near-duplicates/related items for a known test fixture |
| T29 | Duplicate-awareness badge (facet/aggregate count + inline expansion) | M | A 3-copy fixture shows "3 copies" without a separate page load; expansion lists all copies with library/path |
| T30 | EXIF deep extraction (exiftool pool) + GPS default-hidden gate (`expose_gps`) | M | Camera/lens/exposure fields appear in `metadata_`; GPS fields are absent from `/search` responses and Meili facets unless the library's `expose_gps=true` |
| T31 | Tag facet type-ahead endpoint | S | Typeahead returns tag suggestions with counts, typo-tolerant per index default |
| T32 | Archive member listing (zip/7z), guarded | M | Archive contents appear as pseudo-items/facts without unpacking beyond the guarded ratio |
| T33 | Timeline browsing view | S | Date histogram navigable, backed by existing sortable/filterable fields |

Sizes: S = part of a normal sprint slice, M = multi-day with real design decisions,
L = major item needing its own design pass (matches the T-sizing convention already
used across phase-1 docs).

---

## 12. Open questions (need a user/maintainer decision, not further research)

1. **"Open containing folder" mechanism.** A web app cannot reliably open a native
   file manager from a browser tab. Options: (a) omit this action entirely for the
   pure web UI and only support it once the distributed-agent/native-client work
   (roadmap §1/§2, local CLI/web UI) exists, since a local agent *can* shell out;
   (b) a `file://` link (works only for genuinely local browsing of a local mount,
   not the common case of a remote server + remote browser); (c) defer to
   copy-path as the only P0 action and mark "open containing folder" as effectively
   a v2/agent-era feature. Recommend (c) — flag scope §5's P0 bullet as partially
   deferred pending this decision, since it's currently listed as a same-tier item
   with copy-path but has a fundamentally different feasibility profile in a
   pure-web deployment.
2. **Binary quantization on Hannoy** — needs a direct doc/changelog check against
   the actual Meilisearch version in use at implementation time (not confirmed
   supported vs. still Arroy-only in this research pass).
2b. **Meilisearch version drift** — same caveat: verify the actual latest stable
   release directly before locking implementation details to "v1.48.3 behavior,"
   since this research pass could not confirm anything shipped after it.
3. **Embedder throughput on real hardware** — no benchmark found is representative
   of the Proxmox LXC deployment; needs a local benchmark run before
   committing to default worker concurrency for the embed queue.
4. **OCR default-on vs. opt-in per library** — Recoll's per-directory opt-in is the
   recommended pattern, but should the *global* default be off (safer, matches
   Paperless's conservative `skip` mode) or on-with-a-threshold-gate (more useful
   out of the box)? Needs a maintainer call balancing CPU cost against payoff.
5. **GPS opt-in default at the global config level** (`FILEARR_GPS_EXPOSE_DEFAULT`)
   — this brief recommends `false` as the hard default, but confirm no deployment
   scenario (e.g., a single-user fully-private instance) should reasonably default
   to `true`; if such a case exists, it should still require an explicit first-run
   choice, never a silent default.
6. **Federated multi-search** — not needed today (single index), but flag for
   revisit if/when indexes are ever split per media type or per tenant (roadmap
   §3's RBAC/tenant-token work could plausibly motivate this later).
7. **Saved-search ACL model** — depends on roadmap §3 (RBAC) landing; the P1 task
   above (T26) should ship with an owner reference from day one even if enforcement
   is deferred, to avoid an awkward later migration.
