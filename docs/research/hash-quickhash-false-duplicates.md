# Research Brief — quick_hash False Duplicate Detections (roadmap §16)

Scope: `docs/future-roadmap.md` §16, "RESEARCH PHASE REQUIRED (user-reported
2026-07-16): quick_hash produces thousands of false duplicate detections on
SMALL files." Constraint order for every tradeoff below: **security >
integrity > reliability > speed > compatibility > scalability**. Researched
2026-07-18 against the live deployment (`https://filearr.example.com`,
read-only GET reproduction) and the actual backend/agent code, not a
reimplementation. No production code was changed for this brief.

---

## 0. tl;dr

**Root cause confirmed and reproduced against the real function.**
`filearr.tasks.extract.quick_hash()` has a genuine off-by-boundary defect,
independent of and in addition to the sampling-window collision the roadmap
already anticipated as "expected" for large padded/templated files:

```python
QUICK_CHUNK = 65536  # 64 KiB head + tail

def quick_hash(path: str, size: int) -> str:
    h = xxhash.xxh3_64()
    with open(path, "rb") as f:
        h.update(f.read(QUICK_CHUNK))       # reads AT MOST 65536 bytes
        if size > QUICK_CHUNK * 2:           # only fires above 131072 bytes
            f.seek(-QUICK_CHUNK, 2)
            h.update(f.read(QUICK_CHUNK))
    return h.hexdigest()
```

For any file with `65536 < size <= 131072` (64 KiB, 128 KiB], the guard
`size > QUICK_CHUNK * 2` is **false**, so the tail branch never runs — but
the head read is still capped at exactly `QUICK_CHUNK` (65536) bytes. The
remaining `size - 65536` bytes (up to ~64 KiB) of the file are **never
read**. Every doc comment in the codebase (`models.py:246`,
`move.py:13`, the roadmap text itself) asserts "for files ≤128KiB it covers
the whole file" — that assumption is false for the code as written; it is
only true for `size <= 65536`. This is a hashing bug, not a grouping bug:
the `(quick_hash, size)` duplicate grouping (`reports.py`, `api/items.py`)
correctly constrains on size and never mixes hash tiers — it just consumes a
`quick_hash` value that is silently wrong for that size band.

**The Go agent mirror has the identical defect**, byte-for-byte on purpose:
`agent/internal/scan/hash.go` `QuickHash()` uses the same `quickChunk*2`
guard and is unit-tested for parity *against* Python's (buggy) output
(`hash_test.go`), so the two implementations agree with each other and are
wrong together.

**Recommended fix:** below a 128 KiB (`2×QUICK_CHUNK`) size floor, stop
sampling — compute a genuine full-file hash unconditionally (independent of
`hash_policy`) and store it as `content_hash`, not just `quick_hash`. A
micro-benchmark against the real `quick_hash`/`full_hash` functions shows
this is not a speed tradeoff: a correctly-sized full read of a small file is
as cheap as (often cheaper than) the current sampled read. Recommended
algorithm for the routine scan-time `content_hash`: upgrade from **xxh3-64
to xxh3-128** (same throughput, far larger collision margin), not BLAKE3 or
SHA-256 — see §6.

---

## 1. The quick_hash contract, as implemented

`backend/filearr/tasks/extract.py:27,75-90`:

```python
QUICK_CHUNK = 65536  # 64 KiB head + tail

def quick_hash(path: str, size: int) -> str:
    h = xxhash.xxh3_64()
    with open(path, "rb") as f:
        h.update(f.read(QUICK_CHUNK))
        if size > QUICK_CHUNK * 2:
            f.seek(-QUICK_CHUNK, 2)
            h.update(f.read(QUICK_CHUNK))
    return h.hexdigest()

def full_hash(path: str) -> str:
    h = xxhash.xxh3_64()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()
```

`quick_hash` is **always** computed at extract time
(`tasks/extract.py:462`); `content_hash` (`full_hash`) is gated by T7's
resolved hash policy (`hashpolicy.resolve_hash_policy`) — `quick_only`
libraries (the default for `auto` over a detected network mount) never get
`content_hash` at all, regardless of file size. This means the coverage gap
below hits hardest exactly where the roadmap's live report came from: SMB
libraries under `quick_only`, where `quick_hash` is the *only* signal the
duplicate surfaces have.

Three distinct size regimes exist in the current code, only one of which
behaves as every docstring in the repo claims:

| size range | what `quick_hash` actually reads | coverage |
|---|---|---|
| `size <= 65536` (≤64 KiB) | one `read(65536)` call, naturally truncated to EOF | **whole file** — correct |
| `65536 < size <= 131072` (64–128 KiB) | one `read(65536)` call only (head), tail branch does not fire | **only the first 65536 bytes** — BUG, silently partial |
| `size > 131072` (>128 KiB) | head 64 KiB + tail 64 KiB | sampled by design (expected, roadmap-anticipated collision risk for padded/templated formats) |

The middle row is the defect. It is exactly the size band a large fraction
of "small file" catalog content falls into: JPEGs from phone cameras/social
apps, PDFs, small documents, artwork/thumbnail images, `.settings`/`.resx`
scaffolding files, etc.

---

## 2. Reproduction against the real functions

Script: `qhash-research/repro_boundary_bug.py` (run via
`backend/.venv312/Scripts/python.exe`, importing `filearr.tasks.extract`
directly — not a reimplementation). Five synthetic cases, each isolating one
hypothesis from the roadmap:

| case | setup | `quick_hash` equal? | `full_hash` equal? | verdict |
|---|---|---|---|---|
| A: size=100000 (bug zone), identical first 65536 bytes, **different** remaining ~34 KiB | two files | **True** | False | **CONFIRMED BUG** — false duplicate, real bytes differ |
| B (control): size=50000 (≤64 KiB), fully covered by the single read | two random files | False | — | correct — no bug in this regime |
| C (control, "expected" collision): size=300000 (>128 KiB), identical head+tail windows, different middle | two files | True (by design) | False | inherent sampling-window collision — the *anticipated*, lower-severity, still-real limitation of any fixed-window sampled hash; not the bug this brief targets |
| D: size == 131072 exactly (the boundary itself) | two files | **True** | (not checked) | boundary is off-by-one too: `size > 131072` excludes the exact 128 KiB point, so it's also in the bug's blast radius |
| E: size == 131073 (one byte past the guard) | two files | False | — | confirms the fix point: as soon as the tail branch fires, differing bytes are covered again |

Output (abbreviated):
```
=== Case A: size=100000 (64KiB < size <= 128KiB), same head, DIFFERENT tail ===
quick_hash a = 30f2bca428db42ff
quick_hash b = 30f2bca428db42ff
quick_hash EQUAL: True  <-- if True, this is a FALSE duplicate
full_hash  EQUAL: False  (should be False -- bytes really differ)
```

Conclusion: **the live-reported false positives are explained by a genuine
hashing defect** (partial-file coverage for 64–128 KiB files), not by the
grouping logic (verified separately, §4) and not solely by the inherent
sampling-window collision the roadmap flagged as a secondary, expected
hypothesis for files >128 KiB.

---

## 3. Live data (production catalog, ~1.09M indexed items, read-only GETs)

`GET /api/v1/reports/duplicate_files?format=ndjson` returned **126,844**
duplicate groups (`qhash-research/dup_report_full.ndjson`, 38.7 MB). Split
by grouping tier:

- **8,399 groups** keyed by `content_hash` (full-hash-confirmed exact
  duplicates — trustworthy).
- **118,445 groups** keyed by the `(quick_hash, size)` fallback (quick-only
  libraries — `pictures`, `documents`, parts of `Shows`/`Music_Lossless`),
  totalling **258,902** item-copies.

Breaking the quick-only groups down by the size band that matters for this
bug (`group_size()` parsed out of the `quick_hash:size` fallback dup_key):

| size band | groups | share |
|---|---|---|
| ≤64 KiB (exact, correct regime) | 4,383 | 3.7% |
| **64–128 KiB (BUG ZONE)** | **3,930** | **3.3%** |
| >128 KiB (sampled-by-design regime) | 110,132 | 93.0% |

### 3a. The bug-zone (64–128 KiB) sample

Classifying all 3,930 bug-zone groups by whether member basenames normalize
to the same name (stripping `" (1)"`/`" (2)"`-style numbering — the classic
"same file downloaded/synced twice" signature):

- **2,909 (74%)** same normalized basename — plausible genuine duplicates
  (numbered browser-download copies, `... - Copy.jpg`, repeated wallpaper
  filenames across category folders). quick_hash's *correct* behavior on
  ≤64 KiB files would have flagged these too; the bug doesn't invalidate
  them, it just means we can no longer trust the signal in this band.
- **1,021 (26%)** *different* basenames sharing a `quick_hash`. Manually
  inspected samples (see `qhash-research/analysis_output.txt`) are mostly
  **also plausibly real duplicates** by context — e.g. the same photo
  synced into `Erics Mobile Photos/DCIM/…`, `Photos/2024/06/…`, and
  `PlexUpload/…` under different photo-management naming conventions; a show's
  `banner.jpg` reused as `season06-banner.jpg` (missing season art commonly
  falls back to the show default in *arr-style libraries). **We could not
  conclusively prove any single live cluster is a genuine false positive
  from metadata alone** — that requires the actual bytes, which this
  research explicitly may not read. What §2's synthetic reproduction proves
  is that the *mechanism* is real and will silently merge genuinely
  different files whenever their first 64 KiB happens to coincide (far more
  likely for structured formats — JPEG/PNG/PDF headers, common EXIF
  thumbnail blocks, office-document container boilerplate — than for
  arbitrary bytes).

### 3b. A concrete, unambiguous false-positive artifact: zero-byte files

One `duplicate_files` row groups **3,711 copies** of `size=0` files
(`.nfo` stub files, 3D-print attachment placeholders, etc. — a Family Guy
episode `.nfo`, a Doctor Who episode `.nfo`, and an unrelated `.stl`
attachment all landed in the same "duplicate" group). This is not the
64–128 KiB hashing bug — every empty file legitimately shares
`quick_hash("") + size=0` — but it **is** a second, independent
false-positive source the interim UX should account for: byte-identical
does not imply *meaningfully* duplicate when the shared content is empty.
It alone accounts for more of the "thousands of false duplicate
detections" perception than the 64–128 KiB band does (3,711 in one row vs.
~1,021 candidate groups spread across thousands of rows). Recommend
excluding `size == 0` (and optionally a small `size < N` floor, e.g. a few
bytes) from the duplicate report/badge entirely — it is cheap, unambiguous,
and orthogonal to the hashing fix below.

### 3c. Deployment scale note

CLAUDE.md's "current state" section (dated 2026-07-06) describes a
1,190-file smoke-test catalog. The live catalog queried here holds **~1.09M
indexed documents** (`GET /api/v1/stats`: `meili.document_count = 1086890`;
`by_type` sums to ~1.08M across video/audio/image/document/other) — this is
now a real, large personal-media + backup catalog, not the original smoke
test. Worth a one-line CLAUDE.md refresh independent of this brief.

---

## 4. Grouping logic verification — ruling out the other roadmap hypotheses

Read `reports.py:_build_duplicates` and `api/items.py:_copy_group_filter`
directly:

```python
dup_key = func.coalesce(
    Item.content_hash,
    Item.quick_hash.concat(literal(":")).concat(cast(Item.size, Text)),
).label("dup_key")
...
.where(_ACTIVE, or_(Item.content_hash.isnot(None), Item.quick_hash.isnot(None)))
.group_by(dup_key)
```

- **Size IS constrained within a quick-only group** — the fallback key is
  literally `quick_hash || ':' || size`, so two files with the same
  `quick_hash` but different `size` land in different groups. This
  hypothesis (from the roadmap's "or from the grouping logic itself")
  is **ruled out**.
- **Hash tiers never mix.** `COALESCE` picks `content_hash` when present,
  else the quick fallback; a `content_hash` value and a `"<quick>:<size>"`
  string live in disjoint value spaces (different lengths/formats), so a
  full-hash-confirmed row can never silently land in the same group as a
  quick-only row. **Ruled out.**
- `api/items.py:_copy_group_filter` (the P3-T10 inline "N copies" badge)
  independently confirms this: it explicitly partitions
  `(Item.content_hash.is_(None)) & (Item.quick_hash == …) & (Item.size == …)`
  and already returns a `match: "content_hash" | "quick_hash" | "none"`
  label — the per-item copies endpoint **already tells the tier**; only the
  canned `duplicate_files` report doesn't surface it explicitly as a column
  (see §7).

So the entire live false-positive signal traces back to §1–2's hashing
defect (plus the independent zero-byte artifact in §3b), not to the
duplicate-surface's SQL.

---

## 5. Size-floor design

### 5a. Is a full hash of a small file actually cheaper than the sampled read?

Benchmark (`qhash-research/benchmark_hashes.py`, best-of-20, this host, NTFS
local disk, no filesystem cache cold-start — files pre-written before
timing):

```
      size  quick_hash best(ms)   full_hash best(ms)  xxh3_64 full best(ms)
      1024               0.0388               0.2625                 0.2685
      4096               0.0360               0.2563                 0.2643
     32768               0.0380               0.2681                 0.2691
     65536               0.0460               0.2666                 0.2612
    100000               0.0340               0.2570                 0.2727
    131072               0.0341               0.2822                 0.2772
    262144               0.0483               0.2931                 0.2762
   1048576               0.0435               0.3019                 0.2908
```

Surprising finding — **the existing `full_hash()` is ~6-8x slower than
`quick_hash()` on small files, but not because it reads more bytes.** Both
functions do the same amount of I/O for a small file (one `read()` call,
naturally truncated at EOF). The gap is `full_hash`'s
`f.read(1 << 20)` — requesting a 1 MiB buffer regardless of actual file
size incurs a large-buffer allocation cost on every call. Isolated
confirmation (`qhash-research/verify_buffer_hypothesis.py`, same file sizes,
three read strategies):

```
    size   1MiB-chunk(ms)  64KiB-chunk(ms)  single-read(ms)
    1024           0.2674           0.0360           0.0401
  131072           0.2788           0.0434           0.0471
```

Requesting a buffer sized to the actual data (either `f.read()` with no
argument, or a chunk size capped near the file's own size) removes ~85% of
the small-file overhead and lands full-file hashing **at parity with, or
cheaper than, the current buggy `quick_hash`** for every size tested up to
128 KiB. **This directly satisfies the roadmap's premise** ("full xxh3/xxh128
of a ≤128KiB file is faster than two seeks") — confirmed, with the added
finding that the *implementation detail* (buffer sizing) matters as much as
the algorithmic approach.

### 5b. Recommended floor: 128 KiB (`2 × QUICK_CHUNK`)

Keep the constant already implicit everywhere in the codebase's own
documentation (`models.py:246`, `move.py:13`) rather than inventing a new
number:

- **`size <= 131072` (128 KiB):** compute a genuine full-file hash and store
  it as `content_hash` **unconditionally, independent of `hash_policy`**
  (quick_only libraries currently never get a `content_hash` at any size —
  this is the actual bug-fix: give small files exact identity regardless of
  network-cost policy, since §5a shows it costs nothing extra locally, and
  over SMB a full read of ≤128 KiB is one small read RPC more than the
  current partial read — the round-trip/open-handshake cost dominates for a
  file this small either way, not the extra bytes transferred). Also keep
  writing `quick_hash` (cheap, and downstream code/tests already depend on
  it being non-null) but it becomes redundant with `content_hash` in this
  band — no correctness cost either way.
- **`size > 131072`:** unchanged — `quick_hash` stays the fast head+tail
  sample for move-detection tier 1; `content_hash` stays gated by the
  existing T7 `hash_policy`/`hash_full_max_bytes` ceiling. The inherent
  sampling-window collision risk for large padded/templated files (case C,
  §2) is real but **out of scope for this brief** — it is the
  already-understood, order-of-magnitude-lower-severity cost of any
  fixed-window sampled hash, not a bug, and the roadmap's own wording
  treats it as a separate, accepted hypothesis.

Why not push the floor higher (e.g. 1 MiB)? Local throughput is not the
constraint above 128 KiB — SMB I/O is (§6b) — and 128 KiB is the size the
codebase already committed to conceptually; raising it would mean hashing
(and, for `quick_only` network libraries, transferring) meaningfully more
bytes per file for a benefit (exact identity vs. sampled) that the >128 KiB
regime already accepts as a tradeoff for every larger file today.

---

## 6. Full-file hash candidate benchmark

`qhash-research/benchmark_hashes.py`, same host (AMD Ryzen 9 9950X3D),
local NTFS disk, best-of-N wall time, `1 << 20`-chunked streaming for the
larger sizes (no small-buffer bias — this table intentionally uses the
*fair*, size-appropriate reads per §5a for every candidate):

| size | xxh3-64 (MiB/s) | xxh3-128 (MiB/s) | BLAKE3 (MiB/s) | SHA-256 (MiB/s) |
|---|---:|---:|---:|---:|
| 4 KiB  | 15.0 | 15.1 | 14.6 | 14.6 |
| 64 KiB | 235.5 | 234.1 | 231.1 | 216.6 |
| 1 MiB  | 3,240 | 3,291 | 2,862 | 1,539 |
| 100 MiB | 4,284 | 4,194 | 3,321 | 1,657 |
| 1 GiB  | 3,343 | 3,320 | 2,774 | 1,484 |

(Small-size throughput numbers are dominated by fixed per-call overhead, not
algorithm speed — that's expected and matches §5a; they're included for
completeness, not as the basis for a size-tier decision.)

**Recommendation: upgrade the routine scan-time `content_hash` from xxh3-64
to xxh3-128, not BLAKE3 or SHA-256.**

- xxh3-128 is **within 1-3% of xxh3-64's throughput** at every size tested
  (confirmed above) — the wider digest is effectively free.
- `Item.content_hash` is a `Text` column (`models.py:248`), so widening the
  hex digest from 16 to 32 characters is a **zero-migration, drop-in**
  change (no column-width DDL).
- The 65536-item catalog... at production scale (~1.09M items live, §3c)
  xxh3-64's 64-bit space gives a birthday-bound collision probability of
  roughly `n²/2^65 ≈ (1.09×10^6)²/2^65 ≈ 3.2×10⁻⁸` — already small, but
  xxh3-128 pushes that to astronomically negligible (`~2^-64` smaller again)
  at no throughput cost, which matters more as the catalog keeps growing
  (§3c already shows ~900x growth since the last documented count).
- BLAKE3 (~15-20% slower than xxh3 at scale) and SHA-256 (~2.2-2.9x slower)
  are **cryptographic-strength** hashes — appropriate for the *existing*,
  separate on-demand `hashx.compute_digests()` path (P3-T1's lazy
  `GET /items/{id}/hash?algo=…` endpoint, already scaffolded, already uses
  `hashlib` md5/sha256) where an adversarial/verification use case is
  plausible (a user hand-verifying a download against a published
  checksum). The routine scan-time `content_hash`/`quick_hash` pair has no
  adversarial threat model in this codebase's invariants (nobody is
  engineering a crafted xxh3 collision against a home media catalog's own
  internal dedupe) — paying SHA-256's ~2-3x throughput cost catalog-wide for
  every scan buys no real security here, only slower scans. BLAKE3 isn't
  wired into `hashx.py`'s `hashlib.new()`-based dispatch today either (it's
  not a `hashlib` algorithm) — adding it would be a separate, small
  follow-up if a faster crypto-strength option is ever wanted there, out of
  scope for this brief.

### 6a. SMB throughput — cannot be measured from this host, reasoned analytically

The live deployment's `quick_only` libraries are SMB-mounted (rclone, per
CLAUDE.md); this sandbox has no LAN path to the Proxmox/Unraid boxes, so no
real SMB throughput number can be measured here — flagged as a limitation,
not glossed over. Reasoning instead from known bounds: typical consumer SMB
over 1 GbE tops out around ~110 MB/s; even a well-tuned 10 GbE SMB share
rarely sustains more than a few hundred MB/s to ~1 GB/s end-to-end (protocol
overhead, single-stream limits, real-world NAS/rclone-mount latency).
**Every candidate above (1,484-4,284 MiB/s at 100 MiB+) exceeds realistic
SMB throughput by 3-40x even before counting hashing as fully overlappable
with I/O wait** — confirming CLAUDE.md's existing framing (`quick_only`
exists because SMB/NFS scans are I/O-bound, not because any of these hash
functions are slow). This reinforces §5b: the §5 fix's real cost over SMB is
the *extra bytes read* for small files (negligible — going from a 64 KiB
partial read to a ≤128 KiB full read), not the hash algorithm; and it
reinforces §6's pick of xxh3-128 over BLAKE3/SHA-256 for the >128 KiB
sampled tier too, since even there hashing is not the bottleneck and there
is no reason to spend the extra CPU.

---

## 7. Interim duplicate-report UX requirement (until the fix lands)

The roadmap requires stating which hash tier grouped each cluster.
`api/items.py`'s `/items/{id}/copies` endpoint **already does this**
(`match: "content_hash" | "quick_hash" | "none"`, `api/items.py:550-563`).
The canned `duplicate_files` report (`reports.py:403-447`) does **not**
surface an explicit tier column today — it returns both `content_hash` and
`quick_hash` per row (one is always `NULL`), so a caller can infer the tier
but must do so client-side. Minimal interim fix: add a computed
`hash_tier: "content_hash" | "quick_hash"` column to `_row_duplicates()`
(`CASE WHEN r.content_hash IS NOT NULL THEN 'content_hash' ELSE 'quick_hash'
END`) and surface it in the report UI/export next to the copy count, with a
tooltip/legend noting that `quick_hash` groups are a sampled signal (not
byte-verified) until §5's fix ships. Also apply §3b's `size == 0` exclusion
immediately — it is independent of the hashing fix and removes the single
largest (3,711-copy) false-positive artifact in the live data today.

---

## 8. Go agent exposure — confirmed, same defect

`agent/internal/scan/hash.go:35-70`:

```go
// QuickHash computes the xxh3_64 hex digest over the first 64 KiB (always) plus
// the last 64 KiB (ONLY when size > 128 KiB), fed to a single streaming hasher.
// Byte-for-byte parity with extract.quick_hash is enforced by hash_test.go
// against Python-precomputed digests.
func QuickHash(pathStr string, size int64) (string, error) {
	...
	if size > quickChunk*2 {
		...
	}
	...
}
```

The Go implementation intentionally mirrors the Python one byte-for-byte
(cross-language parity tests in `agent/internal/scan/hash_test.go` compare
against Python-precomputed digests), so it inherits the identical
64–128 KiB partial-read defect. `agent/internal/scan/move.go`'s
`hashSizeKey{quick, size}` move-detection tier uses the same
`(quick_hash, size)` key as central's `move.py`, so agent-side move
detection has the same false-positive exposure central's does — an agent
managing a local (non-quick_only-by-default, per `hash.go:24-33`) tree still
computes `QuickHash` for every file and could mis-key a move match in the
64-128 KiB band even when `content_hash`/`FullHash` is also being computed
(the bug is in the *value* stored, not in whether the tier is gated). **Any
fix to `quick_hash`/`QuickHash` must land in both places to stay
byte-for-byte consistent**, and the existing parity test suite
(`hash_test.go`) must be re-baselined against the corrected Python output,
not just re-run — it currently encodes the bug as the "correct" answer.

---

## 9. Migration story for already-stored quick_hash values

There is no dedicated `hash_policy_version` field; the closest existing
machinery is `filearr.provenance.policy_version()`
(`backend/filearr/provenance.py`), a `cfg1:<16-hex>` fingerprint of a
library's **scan-relevant config** (`hash_policy`, `hash_full_max_bytes`,
the global ceiling, `root_path`, inclusion controls), stamped onto
`Item.policy_version` at every extract (`tasks/extract.py:459-460`).

**Gap:** `policy_version` fingerprints *configuration*, not *hashing
implementation behavior*. Fixing the `quick_hash`/`full_hash` code without
changing any library's `hash_policy`/`hash_full_max_bytes`/`root_path`
leaves every existing item's `policy_version` fingerprint **unchanged** —
the fix would silently ship with no mechanism to invalidate or re-hash the
~1.09M already-stored (partially wrong, in the 64-128 KiB band) `quick_hash`
values. The scan-time self-heal path
(`tasks/scan.py:561-563`, "re-queued by the NEXT scan's in-walk self-heal
branch") only re-queues items whose `quick_hash IS NULL` — it does not know
the difference between "never hashed" and "hashed under a since-fixed buggy
algorithm."

**Recommended design (bump + lazy re-hash, consistent with the `cfg1`
scheme-versioning the module already documents):**

1. Add a `HASH_IMPL_VERSION` constant to `provenance.py` (or fold hashing
   behavior into the `_SCHEME` bump: `cfg1` → `cfg2`) and include it in
   `_canonical()`'s payload. Any hashing-algorithm change (this fix, or any
   future one) is then guaranteed to change every item's `policy_version`
   fingerprint even when the *library config* itself hasn't changed — this
   is exactly the "extending the input set later bumps the prefix" case the
   module's own docstring (`provenance.py:20-23`) already anticipates.
2. On deploy, existing items keep their **stale** `policy_version` (a
   `cfg1:…` value); newly-extracted/rescanned items get the new
   `cfg2:…` fingerprint. This makes "was this item hashed under the fixed
   algorithm" a simple, indexable predicate (`policy_version LIKE 'cfg2:%'`
   or a scheme-prefix check) without a data migration pass.
3. Add a lazy re-hash mechanism that does NOT depend on `quick_hash IS
   NULL`: a maintenance task (mirroring the existing `worker.py` periodic
   purge/reconcile pattern) that finds active items whose `policy_version`
   scheme is behind current and whose `size` falls in the previously-buggy
   64-128 KiB band (the fix is precisely targeted — no need to re-hash the
   >128 KiB or ≤64 KiB regimes, which were never wrong), and re-enqueues
   them through the normal extract path (which will recompute both hashes
   correctly and re-stamp `policy_version`). Bound it the same way T7 bounds
   full-hash computation (rate-limited, cancellable, low priority) so a
   ~4,000-group backlog (§3a's bug-zone count) doesn't spike the extract
   queue on deploy.
4. `custom_fields.py:76` already treats `policy_version` (alongside
   `source_agent_id`/`replication_seq`/`source`) as a reserved/system field
   name — no conflict with this plan.

### 9.2 Agent-side migration — KNOWN LIMITATION (stale hashes persist until mtime change)

Confirmed against `agent/internal/scan/scan.go`: the walk/diff re-hashes a file
ONLY when `item.Size != e.Size || item.MtimeNs != e.MtimeNs` (the "changed"
branch), or when `item.QuickHash == ""` (the null-hash self-heal). It does **not**
re-hash a file whose size and mtime are unchanged. So after the QH-T1 fix ships to
an agent, a 64-128 KiB file that is NOT otherwise touched keeps its **stale
(buggy) `quick_hash`** in the agent's local index until its mtime changes; only
then does the rescan emit a `modified` event carrying the corrected hash. This is
the intended agent-side migration for files that DO change, and per the architect
ruling we deliberately do **not** engineer a special agent-side sweep. **Honest
limitation:** for a stable 64-128 KiB file whose mtime never changes, the agent's
stored `quick_hash` stays stale indefinitely — its central copy is corrected only
when the file is modified/moved, or when an operator forces a full agent re-index.
Central's own catalog converges independently via the QH-T4 sweep (agent-owned
libraries excluded there by ruling, precisely because central cannot open the
files — the agent is the only writer for those rows).

---

## 10. Task breakdown

Sizes: S = normal sprint slice, M = multi-day with real design decisions,
L = major item needing its own design pass (matches the repo's
`docs/tasks/*.md` convention). Suggested prefix `QH-T#` (Quick Hash) to
avoid colliding with the single-fix `FIX-N` hotfix numbering already in use
(`docs/tasks/ui-polish-tasks.md`), since this is a multi-task project, not
one hotfix — though **QH-T1** below is itself hotfix-shaped and a candidate
for a `FIX-17`-style fast-follow if the team wants to ship the narrowest
possible patch first.

> **LANDED 2026-07-18 (QH-T1..T5).** Boundary edge chosen: a file whose
> `size <= 2*QUICK_CHUNK` (**<=131072, inclusive** of the 128 KiB point) is hashed
> IN FULL; only `size > 2*QUICK_CHUNK` (strictly greater) is sampled head+tail.
> The identical `size > QUICK_CHUNK*2` predicate is the sampling gate in BOTH
> languages, so the edge is pinned identically. `content_hash` is now **xxh3-128
> (32 lowercase hex chars)**; `quick_hash` stays **xxh3-64 (16 hex chars)**. See
> the per-task notes below and §9 for the agent stale-hash limitation.

#### QH-T1 — Fix `quick_hash`'s partial-read defect (both languages) — size S
> **DONE.** `backend/filearr/tasks/extract.py:quick_hash` and
> `agent/internal/scan/hash.go:QuickHash` now read the whole file for
> `size <= 2*QUICK_CHUNK` and sample head+tail only above it. Go parity fixtures
> in `hash_test.go` re-baselined against the corrected Python output (8 vectors
> regenerated by importing the real functions via `.venv312`; quick digests for
> 65537/100000/131072 changed — the bug — and `full` digests widened to 32 hex).
> Case A + Case D added as explicit vectors in `test_hash_policy.py`
> (`test_quick_hash_bug_zone_case_a/_case_d_boundary`) and `hash_test.go`
> (`TestQuickHashBugZoneCoverage`); the distinctness test's 65537-vs-65536
> expectation flipped from equal to differ.
- **Goal:** stop silently under-hashing files in the 64-128 KiB (inclusive
  boundary) band; restore the "identical hash+size implies identical bytes
  for ≤128 KiB files" invariant every docstring already claims.
- **Deliverable:** in `backend/filearr/tasks/extract.py`, change the guard
  so any file `<= 2*QUICK_CHUNK` is read in full (e.g. `f.read()` with no
  size argument once `size <= QUICK_CHUNK*2`, matching §5a's finding that a
  size-appropriate single read is cheap) rather than a fixed 65536-byte
  head-only read; fix `size > QUICK_CHUNK*2` boundary inclusivity to match
  (`>=` vs `>` — confirm the intended inclusive/exclusive edge and apply the
  same edge in Go). Port the identical fix to
  `agent/internal/scan/hash.go`'s `QuickHash`.
- **Accept:** §2's Case A and Case D (both currently `True`/colliding)
  return `False` after the fix; Case B/C/E behavior unchanged (regression
  guard). Re-baseline `agent/internal/scan/hash_test.go`'s Python-precomputed
  fixture digests against the corrected output (they currently encode the
  bug as correct) and add the two boundary cases as explicit test vectors in
  both `test_hash_policy.py`/`hash_test.go`.
- **Deps:** none. Blocks QH-T2/QH-T3 (bump/rehash should ship with, not
  before, the actual fix).

#### QH-T2 — Small-file unconditional full hash (the §5 size-floor fix) — size M
> **DONE.** `extract.extract_item`'s hashing block now computes a real
> `content_hash` for every `size <= 2*QUICK_CHUNK` file regardless of
> `hash_policy` (even `quick_only`) and independent of the T7 ceiling; larger
> files keep the policy+ceiling gate. `full_hash(path, size)` gained an optional
> `size` arg so a small file is read with a single size-appropriate `read()` (no
> 1 MiB over-alloc). Go `hashFile` mirrors the branch. Benchmark spot-check
> (this host, `.venv312`): small-file `full_hash` is at parity with `quick_hash`
> (~0.04 ms at 1 KiB..131072), vs ~0.26 ms for the old fixed-buffer `full_hash`.
> Test: `test_hash_policy.test_extract_quick_only_small_file_gets_content_hash`.

#### QH-T2 — Small-file unconditional full hash (the §5 size-floor fix) — size M
- **Goal:** give files `<= 131072` bytes a real `content_hash` regardless of
  `hash_policy` (including `quick_only`), eliminating sampled-hash risk in
  the size band cheap enough to hash exactly.
- **Deliverable:** in `tasks/extract.py`'s hashing block
  (`item.quick_hash = quick_hash(...)`; `if resolved.compute_content and
  ...`), add a branch: `if item.size <= QUICK_CHUNK * 2: item.content_hash =
  full_hash(item.path)` unconditionally, ahead of/independent from the
  `resolved.compute_content` gate. Use a size-appropriate read (§5a) in
  `full_hash` (or a new small-file helper) to avoid the 1 MiB buffer
  overhead this brief measured. Port the equivalent branch to
  `agent/internal/scan/hash.go`'s `hashFile()`.
- **Accept:** a `quick_only`-policy library's items `<= 131072` bytes get a
  non-null `content_hash` after extract; §5a's micro-benchmark re-run shows
  the new small-file path is not slower than current `quick_hash` at the
  sizes tested; `test_hash_policy.py` gains a case asserting `content_hash`
  is populated for a small file under `quick_only`.
- **Deps:** QH-T1 (ship the correctness fix and the floor together — no
  reason to ship a fixed-but-still-sampled small-file hash as an interim
  step).

#### QH-T3 — xxh3-64 → xxh3-128 upgrade for `content_hash` — size S
> **DONE.** `full_hash` (Python) and `FullHash` (Go) switched to xxh3-128, hex
> as 32 lowercase chars (Python `.hexdigest()`; Go `%016x%016x` of `Sum128()`
> Hi then Lo — big-endian, verified identical). `quick_hash`/`QuickHash` stay
> xxh3-64. No code path in scope assumes a fixed digest length (string equality
> only; 16 vs 32 never falsely equal). **P10-T5 status:** the agent-staging verify
> path (`agent_staging._compute_staged_hashes`) was concurrently refactored to
> delegate to `extract.full_hash(spath, size)` directly, so it AUTOMATICALLY picks
> up xxh3-128 — `test_staging_verify_p10t5.py` passes as-is (34 tests green), no
> code edit needed there. **REMAINING RECONCILE (orchestrator):** the FUNCTION is
> consistent, but STORED legacy data is not — an item cataloged before this fix
> holds a 16-char xxh3-64 `content_hash`, and a staged-upload verify now recomputes
> 32-char xxh3-128, so a legitimate upload of a not-yet-rehashed file fails with a
> (false) `content_hash_mismatch` (content_hash mismatch is fatal — no quick_hash
> fallback). Central rows self-heal via the QH-T4 sweep, but **agent-owned items
> are excluded from that sweep** (ruling) and only re-hash when their mtime changes
> on the agent, so the verify path should **dispatch on `len(stored_content_hash)`**
> (16→recompute xxh3-64 to compare, 32→xxh3-128) to stay correct across the
> transition. Left untouched here per the split-surface rule.
- **Goal:** cheap collision-margin upgrade per §6 (no measurable throughput
  cost, no schema migration).
- **Deliverable:** switch `full_hash()` (and the Go `FullHash`) from
  `xxh3_64` to `xxh3_128`; `quick_hash`/`QuickHash` (the cheap move-detection
  probe) stay `xxh3_64` — no reason to widen a value that's explicitly a
  fast/approximate signal.
- **Accept:** new `content_hash` values are 32 hex chars; existing 16-char
  values remain valid/comparable-by-string (no false equality between old
  16-char and new 32-char digests, since they're different lengths — verify
  no code path assumes a fixed digest length). Benchmark regression test
  (or a documented manual re-run of §6's script) confirms throughput parity
  with xxh3-64 on this change.
- **Deps:** none functionally, but bundle with QH-T4's version bump so old
  and new `content_hash` values are distinguishable via `policy_version`,
  not just length-sniffing.

#### QH-T4 — `policy_version` scheme bump + lazy re-hash task — size M
> **DONE.** `provenance._SCHEME` bumped `cfg1`→`cfg2` and a `HASH_IMPL_VERSION=2`
> marker folded into `_canonical()`'s payload (§9.1). New periodic
> `worker.rehash_small_files` (cron `55 4 * * *`, `queueing_lock`, no retry) +
> `rehash_small_files_now()` re-enqueue a bounded batch (`REHASH_SWEEP_BATCH=1000`
> — a module constant, not a config knob, since `config.py` was out of surface)
> of **active, `size <= 131072`, old-scheme (`policy_version NOT LIKE 'cfg2:%'`
> and NOT NULL)** items through the normal extract path. **Architect ruling
> honored:** agent-owned items (`library.source_agent_id` set) are EXCLUDED; the
> band ceiling means no >128 KiB item is ever re-hashed. Idempotent convergence
> (re-extract → `cfg2` → drops out next tick). Tests:
> `test_provenance_p4.test_policy_version_scheme_bump_folds_hash_impl_version`,
> `test_rehash_sweep_qh.py` (targeting + rate-limit + convergence).
- **Goal:** the §9 migration story — make "hashed under the fixed
  algorithm" a queryable fact and clear the ~3,930-group (§3a) backlog of
  stale 64-128 KiB hashes without a blocking data migration.
- **Deliverable:** bump `provenance._SCHEME` (`cfg1` → `cfg2`) and fold a
  `HASH_IMPL_VERSION` marker into `_canonical()`'s payload (see §9.1); a new
  periodic maintenance task (alongside `worker.py`'s existing purge/
  reconcile jobs) that finds active items with `size <= 131072` and a
  `policy_version` on the old scheme, and re-enqueues them through the
  normal extract path, rate-limited/cancellable like existing background
  maintenance work.
- **Accept:** `test_provenance_p4.py`-style test confirms
  `policy_version(...)` changes on the scheme bump alone (no config change);
  a scripted re-run against a seeded DB with pre-bug items shows the
  maintenance task converges the 64-128 KiB backlog to the new scheme within
  its configured rate limit; the task never touches items outside the
  affected size band (no wasted re-hash of already-correct large files).
- **Deps:** QH-T1, QH-T2, QH-T3 (the bump should cover everything this brief
  changes in one scheme increment, not three).

#### QH-T5 — Duplicate-report UX: hash tier + zero-size exclusion — size S
> **DONE.** `reports._build_duplicates` adds a computed `hash_tier`
> (`CASE WHEN max(content_hash) IS NOT NULL THEN 'content_hash' ELSE
> 'quick_hash'`) column (also in the report's `columns` tuple, so it rides
> JSON/CSV/NDJSON/XML/xlsx) and a hard `Item.size > 0` exclusion (zero-byte
> cluster suppressed — the configurable floor was skipped per instruction). The
> `ReportsPage.svelte` duplicate view shows a "sampled signal, not byte-verified"
> caveat banner. Tests: `test_reports_p11.test_duplicate_files_hash_tier_column`,
> `test_duplicate_files_excludes_zero_byte`.
- **Goal:** §7's interim UX requirement, shippable independently and
  immediately (does not wait on the hashing fix).
- **Deliverable:** add a `hash_tier` column to `reports.py`'s
  `_row_duplicates()`/`duplicate_files` report (`CASE WHEN content_hash IS
  NOT NULL THEN 'content_hash' ELSE 'quick_hash' END`); exclude `size = 0`
  (and consider a small configurable floor, e.g. `size < 16`) from
  `_build_duplicates()`'s `WHERE` clause; surface the tier + a "sampled
  signal, not byte-verified" note in the UI's duplicate badge/report view.
- **Accept:** `duplicate_files` JSON/CSV/ndjson output includes `hash_tier`
  for every row; the live zero-byte 3,711-copy cluster no longer appears in
  a re-run against a snapshot with that data; `test_reports_p11.py` gains a
  case for both.
- **Deps:** none — ship first, independent of QH-T1..T4.

---

## Appendix — scripts and raw outputs

All in `qhash-research/` (scratchpad, not committed):
- `repro_boundary_bug.py` — §2 reproduction, run against
  `backend/.venv312`.
- `dup_report.json` / `dup_report_full.ndjson` — live
  `GET /api/v1/reports/duplicate_files` pulls (JSON page + full ndjson
  export), 126,844 rows.
- `analyze_dup_report.py` / `analysis_output.txt` — §3's live-data
  classification (size-band buckets, same/different-basename split,
  zero-size cluster).
- `benchmark_hashes.py` / `benchmark_output.txt` — §5a/§6 benchmark
  (quick_hash vs full_hash small-file cost; xxh3-64/xxh3-128/BLAKE3/SHA-256
  across 4 KiB-1 GiB).
- `verify_buffer_hypothesis.py` — §5a's buffer-size follow-up isolating the
  1 MiB-chunk allocation cost from actual I/O cost.
