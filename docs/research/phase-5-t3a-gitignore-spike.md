# P5-T3a — Go gitignore-negation library bake-off (Sonnet, 2026-07-16)
Gates P5-T3. Mirrors the rigor phase-2 §2 / P2-T2 applied to Python's
`pathspec`.

> **ARCHITECT RULING (ratified 2026-07-16):** adopt `git-pkgs/gitignore`
> v1.2.0 as recommended. Conditions: (1) exact pin, never `@latest` — the
> `go.sum` hash freezes the audited code; (2) P5-T3 ships the 44-vector
> table + full P2-T2 case set as a permanent Go test gate — any dependency
> bump must re-pass it; (3) contingency if upstream vanishes or regresses:
> vendor the v1.2.0 code under its MIT license (or port it) — the vector
> gate makes that swap verifiable. Rationale: integrity (correct on real
> shipped presets, sole 44/44) outranks adoption-comfort in the standing
> priority order, and a ~0-dep leaf package with a pinned hash has a smaller
> supply-chain surface than the heavier, *less correct* alternatives.
> The prune-then-descend walk requirement (§ divergence note) is BINDING
> for P5-T3's Go walker.

## Verdict

**Adopt `github.com/git-pkgs/gitignore` v1.2.0**, pinned to the exact version
(not `@latest`), with P5-T3 shipping its own ported copy of this spike's
44-vector table (plus the full P2-T2 case set) as a permanent Go test file —
a compatibility gate any future dependency bump must pass, not a one-time
check. It is the only candidate that passed **44/44** vectors, including
every case that disqualified an established alternative (the literal
`Icon[\r]` macOS pattern, the `$RECYCLE.BIN/` preset, gitignore
comment-line handling). License MIT (AGPL-3.0-compatible).

**Explicit judgment call for the architect:** `git-pkgs/gitignore` is new
(first tag Feb 2026, current v1.2.0 May 2026) and has effectively no
external adoption (2 GitHub stars, 0 forks, single/small maintainer, no
visible CI badge) — a materially different risk profile than `pathspec`
(MPL-2.0, years of production use, the library already trusted for the
central scanner). It is recommended anyway because it is demonstrably the
*most correct* implementation tested, its README states conformance testing
against git's own `wildmatch.c` test suite, and its version history shows it
already fixing the exact class of bug (bracket-expression/escape-sequence
handling) that trips up the older, more popular libraries. The mitigation
is P5-T3 owning a hard regression gate (this vector table) rather than
trusting upstream maturity. **If the architect judges 2-star/no-CI adoption
unacceptable for AGPL-shipped code regardless of correctness, the fallback
is "reimplement the pathspec-equivalent semantics" using `git-pkgs/gitignore`
as a studied reference (not a dependency) — sized similarly to a vendor-and-
own-it plan, since the correct algorithm is now known-good from this spike.**

Runner-up, rejected: `sabhiram/go-gitignore` (43/44 — huge adoption, but its
regex-based engine doesn't escape `$` and silently miscompiles the
`$RECYCLE.BIN/` preset, one of Filearr's five shipped `system_files`
patterns — not a contrived edge case, a disqualifying real-world hit).

## Candidate matrix

| Candidate | Version pinned | License | Maintenance | Vectors passed | Non-stdlib deps | Verdict |
|---|---|---|---|---|---|---|
| `git-pkgs/gitignore` | v1.2.0 (2026-05-19) | MIT | New (first tag 2026-02), 6 releases in ~4mo, 2 stars/0 forks, no visible CI badge | **44/44** | ~0 (leaf pkg) | **Recommended**, adoption risk flagged |
| `sabhiram/go-gitignore` | pseudo-version `v0.0.0-20210923224102` (no tagged release since ~2021) | MIT | Stale (no release in years) but extremely widely vendored (Docker Compose, Helm, etc.) | 43/44 | ~0 (leaf pkg, regex-based) | Rejected — `$`-escaping bug hits a real shipped preset |
| `denormal/go-gitignore` | pseudo-version `v0.0.0-20180930084346` (2018) | MIT | Dormant since 2018 | 42/44 | 1 (`danwakefield/fnmatch`) | Rejected — can't represent `Icon[\r]` (real preset) |
| `boyter/gocodewalker` (vendored+patched `go-gitignore`) | v1.5.1 | MIT | Actively maintained (`gocodewalker` itself), but its bundled gitignore engine is a fork of `denormal` — inherits the same lexer bug | 42/44 | 1 (`danwakefield/fnmatch`) | Rejected — same `Icon[\r]` failure as `denormal` (its parent engine) |
| `go-git/go-git/v5` `plumbing/format/gitignore` | v5.19.1 | Apache-2.0 | Actively maintained (core git implementation, heavy usage) | 42/44 | ~32 (pulls in `go-billy`, `gcfg`, config parsing — the whole-module leaf-package tax) | Rejected — `ParsePattern` has no comment/escape preprocessing when fed in-memory pattern lines directly (only `.gitignore`-file readers get that); plus notably heavier dependency footprint for an agent binary |
| `moby/patternmatcher` (bonus, dockerignore dialect) | v0.6.1 | Apache-2.0 | Actively maintained (Docker/Moby) | 29/44 pass, 11 N/A (no `isDir` concept at all), 4 real fails | ~0 | Rejected outright — dockerignore dialect, confirmed behaviorally non-equivalent to gitignore (anchoring/unanchored-nested matching diverges), not a drop-in |

Go 1.26.5 (sandbox toolchain) resolved all six modules cleanly against
`proxy.golang.org`; `git-pkgs/gitignore` declares `go 1.25.5`, compatible.

## Vector table (44 vectors) and per-candidate result

Ground truth computed against `pathspec==1.1.1` — the exact version pinned
in `backend/pyproject.toml` — via a throwaway script running
`GitIgnoreSpec.from_lines(patterns).match_file(path)`, mirroring
`backend/filearr/presets.py`'s own convention: files are probed as
`spec.match_file(rel)`, directories as `spec.match_file(rel + "/")`
(`presets.prune_dir`). `Path` below is the pathspec-style probe string
(trailing `/` = directory probe); `IsDir`/no-trailing-slash is how each Go
adapter received it (adapters that take an explicit `isDir bool` — `go-git`,
`git-pkgs`'s `MatchPath`, `denormal`/`gocodewalker`'s `Relative` — got the
flag directly; `sabhiram` got the trailing-slash convention it expects
natively).

Legend: **P**=pass (matched pathspec), **F**=fail (diverged), **N/A**=candidate
has no way to express this case (scored separately, not as a failure).
Columns: `denorm`=denormal, `sabh`=sabhiram, `gogit`=go-git,
`gcw`=gocodewalker, `gpkgs`=git-pkgs, `moby`=patternmatcher.

| ID | Patterns | Path (probe) | Expected | denorm | sabh | gogit | gcw | gpkgs | moby | Note |
|---|---|---|---|---|---|---|---|---|---|---|
| V01 | `*.log` | `foo.log` | excluded | P | P | P | P | P | P | basic unanchored glob |
| V02 | `*.log` | `foo.txt` | kept | P | P | P | P | P | P | non-match kept |
| V03 | `*.log`, `!keep.log` | `keep.log` | kept | P | P | P | P | P | P | negation re-include |
| V03b | `*.log`, `!keep.log` | `other.log` | excluded | P | P | P | P | P | P | negation: sibling still excluded |
| V04 | `*.iso`, `!keepme.iso`, `keepme.iso` | `keepme.iso` | excluded | P | P | P | P | P | P | last-match-wins re-exclude after negation |
| V05 | `node_modules/`, `!node_modules/keep.txt` | `node_modules/keep.txt` (file) | **kept** | P | P | P | P | P | N/A | **pathspec/git-DIVERGENCE — see below** |
| V05b | `node_modules/` | `node_modules/` (dir) | excluded | P | P | P | P | P | N/A | dir pattern vs dir probe |
| V06 | `build/` | `build/` (dir) | excluded | P | P | P | P | P | N/A | trailing-slash dir pattern vs dir probe |
| V07 | `build/` | `build` (file-shaped) | kept | P | P | P | P | P | N/A | dir-only pattern must NOT match a file of the same name |
| V08 | `build/` | `build/file.txt` (file) | excluded | **F** | P | P | **F** | P | N/A | dir pattern must match contents — single-call API gap in denormal/gocodewalker (see below) |
| V09 | `/foo.txt` | `foo.txt` | excluded | P | P | P | P | P | **F** | anchored matches top-level |
| V10 | `/foo.txt` | `sub/foo.txt` | kept | P | P | P | P | P | P | anchored does not match nested |
| V11 | `foo.txt` | `foo.txt` | excluded | P | P | P | P | P | P | unanchored matches top-level |
| V12 | `foo.txt` | `sub/foo.txt` | excluded | P | P | P | P | P | **F** | unanchored matches nested too |
| V13 | `a/**/b` | `a/b` | excluded | P | P | P | P | P | P | `**` matches zero segments |
| V14 | `a/**/b` | `a/x/b` | excluded | P | P | P | P | P | P | `**` matches one segment |
| V15 | `a/**/b` | `a/x/y/b` | excluded | P | P | P | P | P | P | `**` matches multiple segments |
| V16 | `a/**/b` | `a/x/y` | kept | P | P | P | P | P | P | `**` non-match when suffix missing |
| V17 | `**/foo.txt` | `foo.txt` | excluded | P | P | P | P | P | P | leading `**` matches top-level |
| V18 | `**/foo.txt` | `deep/nested/foo.txt` | excluded | P | P | P | P | P | P | leading `**` matches any depth |
| V19 | `foo/**` | `foo/bar.txt` | excluded | P | P | P | P | P | P | trailing `**` matches all contents |
| V20 | `foo/**` | `foo/` (dir) | excluded | P | P | P | P | P | N/A | trailing `**` dir probe |
| V21 | `Secret.txt` | `Secret.txt` | excluded | P | P | P | P | P | P | user pattern case-sensitive: exact case |
| V22 | `Secret.txt` | `secret.txt` | kept | P | P | P | P | P | P | user pattern case-sensitive: different case (documented gap, R2) |
| V23 | `[Tt]humbs.db` | `Thumbs.db` | excluded | P | P | P | P | P | P | bracket-expanded builtin, Capitalized |
| V24 | `[Tt]humbs.db` | `thumbs.db` | excluded | P | P | P | P | P | P | bracket-expanded builtin, lowercase |
| V25 | `[Tt]humbs.db` | `THUMBS.DB` | kept | P | P | P | P | P | P | all-caps NOT covered (only the two bracketed variants) |
| V26 | `Icon[\r]` | `Icon\r` (literal CR) | excluded | **F** | P | P | **F** | P | P | **real macOS preset — disqualifying for denormal/gocodewalker (see below)** |
| V27 | `Icon[\r]` | `Icon` | kept | P | P | P | P | P | P | no CR -> no match |
| V28 | `Icon[\r]` | `IconX` | kept | P | P | P | P | P | P | wrong suffix -> no match |
| V29 | `\#*` | `#emacs-autosave#` | excluded | P | P | **F** | P | P | **F** | escaped leading `#` is a real pattern |
| V30 | `#*` | `#emacs-autosave#` | kept | P | P | **F** | P | P | **F** | unescaped leading `#` is a COMMENT line, not a pattern |
| V31 | `$RECYCLE.BIN/` | `$RECYCLE.BIN/` (dir) | excluded | P | **F** | P | P | P | N/A | **real preset — disqualifying for sabhiram (see below)** |
| V32 | `$RECYCLE.BIN/` | `$RECYCLE.BIN` (file-shaped) | kept | P | P | P | P | P | N/A | dir-only pattern vs file-shaped probe |
| V33 | `.Trash-*/` | `.Trash-1000/` (dir) | excluded | P | P | P | P | P | N/A | glob + dir anchor |
| V34 | `._*` | `._resource` | excluded | P | P | P | P | P | P | AppleDouble sidecar prefix |
| V35 | `._*` | `resource` | kept | P | P | P | P | P | P | non-match kept |
| V36 | `.*` | `.hidden_file` | excluded | P | P | P | P | P | P | hidden_dotfiles catch-all |
| V37 | `.*` | `.config/` (dir) | excluded | P | P | P | P | P | N/A | hidden_dotfiles catch-all, dot-dir |
| V38 | `.*` | `keep.mp4` | kept | P | P | P | P | P | P | non-dot file kept |
| V39 | `*.tmp`, `!important.tmp` | `important.tmp` | kept | P | P | P | P | P | P | preset+library include negation |
| V40 | `*.tmp`, `!important.tmp` | `scratch.tmp` | excluded | P | P | P | P | P | P | sibling still excluded |
| V41 | `node_modules/` | `src/node_modules_helper.js` | kept | P | P | P | P | P | N/A | dir pattern must not substring-match a filename |
| V42 | `*.mkv` | `movie.mp4` | kept | P | P | P | P | P | P | include-as-negation: non-excluded non-match is KEPT (P2-T2's old-allowlist-vs-new-negation divergence case) |

**Totals:** git-pkgs 44/44 · sabhiram 43/44 (V31) · go-git 42/44 (V29, V30) ·
denormal 42/44 (V08, V26) · gocodewalker 42/44 (V08, V26, inherits denormal's
engine) · moby 29/44 pass + 11 N/A + 4 fail (V09, V12, V29, V30).

## Root causes of each divergence (traced to source, not just observed)

- **V05 — negation under an excluded directory (pathspec/git-proper
  divergence, not a Go-library bug):** every gitignore-family library tested
  here (including `pathspec` itself) returns **kept** for
  `node_modules/keep.txt` when queried as a single flat `match_file`/`Match`
  call against `["node_modules/", "!node_modules/keep.txt"]` — i.e. the
  negation appears to work. **Real git does not**: per git's own docs (and
  echoed verbatim in `sabhiram`'s and `go-git`'s package comments), "it is
  not possible to re-include a file if a parent directory of that file is
  excluded," because git never even lists files under an excluded directory.
  This is not a library defect — it is a property of calling a path-string
  matcher with a single leaf path instead of walking the tree top-down.
  **`presets.py`/`scan.py` already get this right architecturally**:
  `walk()` calls `prune_dir()` on `node_modules/` *before* ever reaching
  `node_modules/keep.txt`, so the negation is never consulted — directory
  pruning always wins (ruling R1). **The Go agent (P5-T3) must replicate
  this same two-layer order (prune directories top-down during the walk,
  only match files reached by an unpruned descent) — picking a spec library
  alone does not give you git-correct semantics; the walk architecture is
  load-bearing.** This generalizes beyond V05: any one-shot "is this deep
  path excluded" API (as opposed to an incremental per-directory-level walk)
  will systematically disagree with git here, in whichever library is used.
- **V08 — denormal/gocodewalker dir-pattern-vs-contents:**
  `pattern.Match(path, isdir)` in `denormal`'s `name`/`path` types returns
  `false` immediately whenever `_directory && !isdir` — i.e. a directory-only
  pattern (`"build/"`) refuses to match anything that isn't itself queried as
  a directory, so a single flat call for `build/file.txt` (isDir=false) never
  matches even though it is unambiguously "inside build/". This is the same
  class of gap as V05: the library expects the CALLER to already have
  pruned/descended `build/` during a walk, not to answer "is this deep leaf
  under an excluded dir" in one shot. Given the walk architecture Filearr
  already uses (prune-then-descend), this specific gap is largely moot in
  practice — **but it does not explain away V26**, which is a hard parser bug.
- **V26 — denormal/gocodewalker cannot represent `Icon[\r]` at all:** the
  lexer (`lexer.go`) treats a bare `\r` as an end-of-line marker (it defines
  a dedicated `CarriageReturnError`) even when the `\r` is inside a bracket
  expression (`[\r]`), so the literal-CR macOS Finder pattern — one of
  Filearr's five shipped `os_metadata` preset lines — cannot be expressed as
  input at all. This is disqualifying independent of the walk-architecture
  mitigation above.
- **V31 — sabhiram `$RECYCLE.BIN/` miscompile:** `getPatternFromLine` only
  escapes the literal `.` character (`regexp.MustCompile(\.).ReplaceAllString`)
  before compiling the pattern into a Go `regexp`. It does **not** escape
  `$`, which is a regex end-of-string/line anchor in Go's RE2 engine. The
  compiled pattern for `$RECYCLE.BIN/` therefore contains a stray anchor
  where a literal `$` was intended, and the regex can never match the real
  string `$RECYCLE.BIN`. This hits one of Filearr's five shipped
  `system_files` preset patterns directly — not a contrived adversarial input.
- **V29/V30 — go-git comment/escape handling lives outside `ParsePattern`:**
  `ParsePattern(p string, domain []string)` does no comment-stripping or
  backslash-unescaping at all; git-gitignore's `#`-comment and `\#`-escape
  rules are implemented only in the higher-level `.gitignore`-file readers
  (`ReadPatterns`/`LoadGlobalPatterns` etc.) that go-git uses when reading
  real files out of a git worktree/tree object. Since Filearr's presets are
  in-memory Go string literals (never real `.gitignore` files on disk), a
  caller using `ParsePattern` directly must hand-roll that preprocessing
  step itself — an easy-to-forget integration burden, not a hard blocker.
- **V09/V12/V29/V30 — moby/patternmatcher:** confirmed behaviorally
  non-equivalent to gitignore proper (its native dialect is `.dockerignore`,
  which historically diverges on anchoring rules), and it exposes no `isDir`
  parameter at all, so every directory-probe vector is unanswerable (scored
  N/A). Rejected outright, not merely deprioritized.

## Python-reference confirmation notes

Ground truth was generated by `uv run --no-project --with pathspec python
vectors.py` (the coordinator flagged, and this session independently hit
first, that plain `uv sync`/`uv run` inside `backend/` fails on this machine
because the project pins Python 3.13 and `pgserver` has no `cp313` wheel —
`--no-project --with pathspec` sidesteps the whole-project resolve and pulls
only `pathspec` standalone). Installed version: `pathspec==1.1.1`, matching
`backend/pyproject.toml`'s exact pin. All 44 vectors' `matched_excluded`
values were computed directly from `GitIgnoreSpec.from_lines(patterns
).match_file(path)` — the identical call `presets.build_exclusion_spec`
makes — before any Go code was written, so the Go harness was scored against
a pre-computed, independently-reproducible oracle rather than tuned after
the fact.

## Risks

1. **Adoption/maturity risk of the recommended library** (the flagged
   judgment call above) — 2 stars, ~5 months old, no visible CI. Mitigated
   by P5-T3 owning a hard regression gate (this vector table), but the
   architect should explicitly ratify accepting a low-adoption dependency
   for AGPL-shipped code, or direct the fallback (vendor-and-study-only /
   reimplement).
2. **V05-class divergence is structural, not library-specific.** Whichever
   library P5-T3 lands on, the Go agent's walk MUST prune directories
   top-down before matching files underneath them (mirroring
   `scan.py::walk` + `presets.prune_dir`) — a flat "does this deep path
   match" call over any of these libraries will silently disagree with git
   (and with the central Python scanner) on files nested under an excluded,
   negated-into directory. This is an architecture requirement for P5-T3,
   not a library-selection question, and should be called out explicitly in
   P5-T3's design so it isn't rediscovered as a field bug.
2b. **Directory-probe convention must match `presets.prune_dir`'s exactly**
   — probe directories with a trailing-slash-equivalent (or the library's
   native `isDir` flag), never as bare names, or dir-only patterns
   (`node_modules/`, `$RECYCLE.BIN/`, etc.) silently stop matching.
3. **Case-sensitivity gap (R2) must be ported as-is.** `git-pkgs/gitignore`
   was only tested with the same pre-bracket-expanded builtin patterns
   (`[Tt]humbs.db` etc.) central already ships; it was not verified to add
   any independent case-folding behavior central doesn't have. P5-T3 should
   reuse central's exact bracket-expanded pattern strings for the builtin
   presets (not re-derive them) so the two catalogs can't drift on this axis.
4. **Version drift risk.** `git-pkgs/gitignore` is pre-1.0-cadence-feeling
   despite its v1.x tag (6 releases in 4 months, several fixing real
   correctness bugs) — pin the exact version (`v1.2.0`), do not float
   `@latest`, and re-run this spike's vector table before any version bump.
5. **`go-git`'s dependency footprint** (~32 non-stdlib packages including
   `go-billy` and `gcfg` just for the leaf gitignore package, per `go list
   -deps`) is a secondary reason against it even setting aside the V29/V30
   bug — a meaningfully heavier supply-chain surface for a lean,
   security-sensitive agent binary than any other candidate tested (all of
   which are ~0-1 non-stdlib deps).

## What P5-T3 should do

1. Add `github.com/git-pkgs/gitignore v1.2.0` to `agent/go.mod` (exact pin,
   `go.sum` committed).
2. Port this spike's 44-vector table (`vectors.go` in the throwaway harness)
   plus the full case set already locked by
   `backend/tests/test_pathspec_semantics_t2.py` and
   `backend/tests/test_presets_walk_t1.py` into a permanent
   `agent/internal/scan/gitignore_test.go` — this is the compatibility gate
   any future `git-pkgs/gitignore` bump (or central `pathspec` bump) must
   pass, per CLAUDE.md invariant that the central and agent catalogs must
   not silently diverge.
3. Reuse central's exact bracket-expanded builtin preset strings verbatim
   (do not re-derive `[Tt]humbs.db`-style patterns independently) — copy
   `PRESET_BUNDLES`' pattern text from `backend/filearr/presets.py` into the
   Go preset table as a static, commented "kept in sync with
   presets.py — do not hand-edit without re-running the P5-T3a vector
   table" data literal.
4. Implement the Go walk with the SAME two-layer order as
   `scan.py::walk`/`presets.prune_dir`: check directory-only patterns
   (`Matcher.MatchPath(rel, true)`) BEFORE descending, short-circuit descent
   on a match, and only call the file-level matcher on paths reached by an
   unpruned descent. Do not implement directory exclusion as "match the deep
   file path and see if it happens to hit a dir-shaped rule" — Risk #2 above
   is exactly this trap.
5. If the architect rejects the adoption-risk judgment call in the Verdict:
   fall back to a from-scratch port of `pathspec`-equivalent semantics,
   using `git-pkgs/gitignore`'s (MIT, freely reusable) approach as a studied
   reference for the wildmatch algorithm shape rather than as a dependency —
   this spike has already validated that the algorithm is correct against
   all 44 vectors, so the remaining work would be reimplementation effort,
   not a new correctness investigation.

## Harness (throwaway, not committed to the repo)

Scratch Go module at
`gitignorespike` (built under the session scratchpad, per instructions —
NOT added to `d:\repos\filearr`): `vectors.go` (44-entry table with
pathspec-computed expected values), `adapters.go` (one adapter per
candidate normalizing to a common `(excluded bool, applicable bool, err
error)` signature), `main.go` (table-driven runner printing a full
candidate × vector matrix plus a pass/fail/N-A summary). Go 1.26.5, modules
resolved live against `proxy.golang.org`:
`denormal/go-gitignore@v0.0.0-20180930084346-ae8ad1d07817`,
`sabhiram/go-gitignore@v0.0.0-20210923224102-525f6e181f06`,
`go-git/go-git/v5@v5.19.1`, `boyter/gocodewalker@v1.5.1`,
`git-pkgs/gitignore@v1.2.0`, `moby/patternmatcher@v0.6.1`. Python oracle:
`uv run --no-project --with pathspec python vectors.py` against
`pathspec==1.1.1` (the exact `backend/pyproject.toml` pin).
