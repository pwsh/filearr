# Data collected & how

This page is a thorough, honest accounting of **what Filearr reads, computes,
stores, and transmits** — and, just as importantly, what it does **not**.

!!! success "No external telemetry, ever"
    Filearr has **no phone-home, no analytics, no external telemetry**. It talks
    only to the services you configure (your Postgres, your Meilisearch, your
    media mounts, and — if you enable them — your own agents and their CA).
    Meilisearch analytics are disabled in the shipped configuration. The only
    outbound network calls Filearr makes are ones you explicitly turn on: an
    OIDC/LDAP provider you configure, alert webhooks/SMTP you create, an optional
    one-time embedding-model download, and (for the agent CA) Let's Encrypt /
    Cloudflare DNS if you choose the ACME TLS mode.

## What a scan reads

A scan is a **read-only** filesystem walk of a library root. Media mounts are
mounted read-only; Filearr never modifies your files.

### Filesystem walk and `stat`

For every file the walk records: the absolute path (as currently mounted), the
**path relative to the library root** (the stable identity), the filename and
extension, the **size**, and the **modification time**. Classification comes from
the extension via the editable taxonomy: a **file category** (the parent — video,
audio, image, document, …) and a finer **file group** (RAW vs. raster photo,
archive, source code, …) — see the [file-extension groups
reference](reference/file-extension-groups.md).

**Not every file on disk is ingested.** A scan skips files four ways — the
library's category/group selection, the exclusion presets/globs, pruned
directories, and unreadable directories. The first two are counted and shown on
the Libraries page; **pruned and unreadable directories are skipped without being
read at all**, so the files inside them are counted nowhere and the reported
totals are a lower bound. This is why a library can legitimately show far fewer
files than the folder's properties. See [A library indexes fewer files than the
OS reports](operations.md#library-file-count-mismatch) for how to attribute the
difference and how to make the counts reconcile exactly.

### Hashing (xxh3)

Filearr computes non-cryptographic **xxh3** hashes for change- and move-detection:

- **quick hash** — over the **first and last 64 KiB** of the file. Cheap; used as
  the first tier of move detection.
- **content hash** — the full (chunked) file hash, used to disambiguate move
  candidates. There is a size ceiling (`FILEARR_SCAN_HASH_FULL_MAX_BYTES`,
  default 1 GiB) above which the full content hash is skipped.

These are for identity/move-detection, not integrity attestation. **Cryptographic
digests (MD5 / SHA-256) are computed only on demand** via
`POST /api/v1/items/{id}/digests`, which streams the file once and caches the
result — never automatically during a scan, and with a hard size ceiling.

### Per-type extractors

Extraction runs per file, after the scan batch commits, on dedicated worker
queues. What each extractor reads and stores:

| Type | Tool | What it extracts |
|---|---|---|
| Video | `ffprobe` | Codecs, resolution, duration, bitrate, streams, container facts (bounded runtime + output size). |
| Audio / audiobook / sample | tag libraries | Tags (artist/album/title/etc.), duration, channels, sample rate, cover art. |
| Image | `exiftool` | Curated camera / lens / exposure / dimension fields under an `exif.` namespace. **GPS is gated — see below.** |
| Document | pypdf / python-docx / openpyxl | Document properties; optional **body text** for search snippets (bounded, opt-in per feature). |
| Spreadsheet | openpyxl | Workbook properties (metadata only; cell extraction is a future capability). |
| 3D model | trimesh | Geometry facts for safe formats; a lightweight file-fact record for formats with no safe pure loader. |
| Archive | zip/tar readers | **Member name listing** (searchable) *without unpacking*, guarded against zip/decompression bombs. |
| PDF / image / video | Pillow / PDFium / ffmpeg | Content-addressed WebP **thumbnails** + video poster frames (a disposable cache). |

Every extractor is bounded (timeouts, output-size caps, pixel/decompression bomb
guards) so a hostile or oversized file cannot stall or OOM a worker.

!!! warning "GPS coordinates are hidden by default"
    Image EXIF GPS keys are **stored raw** but **stripped from the search index
    and the API** unless the owning library's `expose_gps` toggle is on. There is
    deliberately **no** global default-on path — location data does not leak into
    search results or API responses unless you opt a library in.

!!! note "OCR and semantic embeddings are opt-in"
    - **OCR** (Tesseract) runs only for a library with `ocr_enabled` — the default
      install pays zero OCR cost. When on, it OCRs pages/images with no usable
      text layer, bounded by page/pixel/time caps, storing capped text.
    - **Semantic search** (a local ONNX embedder) is **globally off by default**;
      when enabled it computes dense vectors locally (never a cloud API — private
      files never leave the box) and downloads a ~130 MB model once.

## Extracted metadata vs. user edits (the separation contract)

Filearr keeps two distinct metadata stores on every item:

- **`metadata`** — everything extractors and scans discover. Rescans and
  re-extraction **overwrite** this freely.
- **`user_metadata`** — everything a human edits through the API/UI. Scans and
  extractors **never** write here.

The **effective value** a user sees is `user_metadata` overlaid on `metadata`, so
your manual edits always win and are never clobbered by a rescan. This is
architecture invariant 2 and it is enforced at the API and ORM layers.

## What agents replicate {#what-agents-replicate}

When you run the optional agent fleet, each agent replicates a **narrow,
lightweight** change set to central — never your file contents.

**What leaves the agent machine** (the "R1" replication field set), per changed
file:

- `rel_path`, `size`, `mtime`
- `quick_hash` and `content_hash` (content hash may be null for large/networked
  files)
- a `moved` event carries the old path (delete+create pair)
- an optional, best-effort `share_hint` (a network-share URL/UNC/host, so the
  central UI can offer an "open on the network" link)

**What never leaves the agent machine:**

- **File contents** — never, except an explicit **retrieve** you initiate, or a
  small size-capped **thumbnail** the agent uploads for the grid.
- **Local search history** — the frecency store is a separate local database the
  replication subsystem is architecturally incapable of reading; central holds no
  copy.
- The filename-derived **title** and any local-only fields stay agent-side until
  central enriches the item itself.

Full/heavy metadata extraction happens **centrally** after replication, from the
lightweight events — not on the agent.

## What the search index stores

Meilisearch holds a **projection** of the catalog optimized for search: item
identity and display fields (path, filename, type, size, times), searchable text
(titles, tags, selected metadata, capped document body text and archive member
names when present), facet values, and — when RBAC search is enabled — the item's
path scope for tenant filtering. GPS is excluded unless a library opts in. The
index is **disposable**: every field is rebuildable from Postgres, and it is never
a store of record.

## What audit logs record

The security-events log records login/logout/session lifecycle, grant changes,
and agent lifecycle mutations, plus (unconditionally) data-egress actions
(download/export/verify). Optional read auditing records a per-query event when
enabled. **Raw tokens and secrets are never recorded** — only non-secret
references (e.g. an OTT's `jti`) appear. See
[Security → Audit log](security.md#audit-log).

## Summary: where each kind of data lives

| Data | Store | Leaves your infrastructure? |
|---|---|---|
| Paths, sizes, times, hashes | Postgres | No |
| Extracted metadata (ffprobe/EXIF/tags/…) | Postgres | No |
| Your edits (`user_metadata`, tags, custom fields) | Postgres | No |
| GPS coordinates | Postgres (hidden unless opted in) | No |
| Search projection | Meilisearch (local) | No |
| Thumbnails / posters | Config-volume cache (disposable) | No |
| Agent local search history | Agent-local SQLite | **Never** |
| File contents | Your media (read-only) | Only on explicit retrieve/thumbnail |
| Telemetry / analytics | — | **None exists** |
