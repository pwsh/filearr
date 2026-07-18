# Research Brief — Future Roadmap Item 4: Indexing Controls

Scope: roadmap `docs/future-roadmap.md` §4 (Indexing controls). Interacts with §1
(distributed agent architecture, v3) for the "centrally managed" and "agent
setup default locations" requirements, and with T5 (scheduled + watch-mode
scanning, shipped) for hot-folder scheduling. Written 2026-07-07.

Constraints applied throughout: security > integrity > reliability > speed >
compatibility > scalability. AGPL-3.0-or-later project — dependencies must be
permissive/MIT/BSD/Apache/LGPL/MPL (no AGPL-incompatible proprietary code
recommended; guessit-style LGPL is fine, FileFlows/Tdarr/tinyMediaManager PRO
are cited for pattern only, never as dependencies).

---

## 0. Current state (baseline, verified against the actual codebase)

- `libraries` table (`backend/filearr/models.py`) already has `enabled_types`
  (`ARRAY(Text)`, empty = all types), `include_globs`, `exclude_globs`
  (`ARRAY(Text)`), `native_prefix`, `scan_cron`, `watch_mode`, `hash_policy` +
  `hash_full_max_bytes` (T7). All per-**library**, none per-**subfolder**.
- `backend/filearr/media_types.py` is a **code-constant** `EXT_MAP` (extension
  -> `MediaType` enum), registered via a `_register()` helper. No DB table, no
  per-library override beyond the coarse `enabled_types` (which toggles whole
  `MediaType` buckets, not individual extensions or extension groups).
- `backend/filearr/tasks/scan.py::walk()` matches globs with **stdlib
  `fnmatch.fnmatch`** directly against `rel` (path relative to library root),
  called once per file for `exclude` then once for `include`. No directory
  pruning by glob -- the walk always descends into every subdirectory (the only
  hard-coded skip is `entry.name.startswith(".")`, i.e. **all dotfiles/dot-dirs
  are unconditionally skipped today**, not a toggleable preset). This is a
  correctness note for scoping T4-1 below: today's "hidden/dotfiles" behavior
  already exists, but as a hard-coded, non-optional walk-level skip, not a
  preset a user can disable.
- T5 (`backend/filearr/schedule.py`, `backend/filearr/worker.py`,
  `backend/filearr/watch.py`) shipped: one static Procrastinate periodic task
  (`schedule_scans`, `@periodic(cron="* * * * *")`) evaluates every enabled
  library's `scan_cron` against the tick with **cronsim**, defers due scans,
  skips libraries with a scan already `running`, and a `queueing_lock` of
  `scan:<library_id>` collapses duplicate/racing enqueues. `WatchSupervisor`
  runs watchfiles (`awatch`) per library with a local-root-only guard
  (`schedule.is_network_path`, parses `/proc/self/mountinfo`), debounces bursts,
  and triggers a **normal full scan** (no incremental single-file path -- an
  explicit, documented decision: move/sidecar detection need whole-library
  context). Both `scan_cron` and `watch_mode` are per-**library**, not
  per-subfolder.
- Procrastinate periodic tasks are **import-time static** -- this is the
  standing constraint that forced T5's single-tick-evaluates-all-rows design,
  and it directly shapes the hot-folder scheduling recommendation below (S3).

---

## 1. Prior art: exclusion preset bundles

Every tool surveyed converges on the same two-tier shape: a **directory-prune**
mechanism (stop descending -- a performance requirement, not just filtering) and
a separate **anywhere-matching filename** mechanism. This is strong, convergent
validation for splitting Filearr's exclude config the same way, since prune
patterns let the scanner short-circuit `os.walk`/`scandir` instead of
descending into e.g. `node_modules/` only to filter every file inside it after
the fact.

### 1.1 Everything (voidtools)

Closed-source freeware; docs at `voidtools.com/support/everything/options/` and
`.../searching/`. Cited for UX pattern only (not a dependency). Has "Exclude
Folders" (absolute paths or wildcards, optional `regex:` prefix), an
include/exclude wildcard list, and attribute-based "exclude hidden/system
files" toggles matched against **NTFS `HIDDEN`/`SYSTEM` bits**, not name
patterns -- notable because it means Everything's approach doesn't generalize
to non-NTFS filesystems (network SMB/NFS mounts, exactly Filearr's deployment
target) the way name-pattern exclusion does. Notably `$Recycle.Bin` and
`System Volume Information` are **not excluded by default** in Everything --
users opt in manually. (This cuts against blindly copying "everyone excludes
these" -- worth noting as a design choice Filearr should make deliberately
rather than assume.)

### 1.2 Recoll (GPL-2.0-or-later)

`recoll.org` usermanual + sample `recoll.conf`. Two-tier config, matching the
prune/name split exactly:
- `skippedPaths` -- absolute-path glob (`fnmatch` w/ `FNM_PATHNAME`), i.e. a
  **prune** list. Default ships almost empty (just `/media`).
- `skippedNames` -- bare-name glob matched **anywhere** in the tree (a **name**
  list), with incremental `skippedNames-`/`skippedNames+` override syntax
  (subtract/add without restating the whole list). Default list: `#* *~
  caughtspam tmp loop.ps Cache cache* .cache .ccls-cache .thumbnails .beagle
  CVS .svn .git .hg .bzr .xsession-errors`.
- Separately, `nowalkfn` is a **sentinel-file-based** subtree skip (if a
  directory contains a file with this name, don't walk it) -- this is the same
  idea as restic's `--exclude-if-present` and the CACHEDIR.TAG convention
  below, independently reinvented. Also has `noContentSuffixes` (MIME-based
  skip for content indexing, less relevant to Filearr's property-only v1
  extraction).

### 1.3 sist2 (GPL-3.0) -- closest spiritual precedent to Filearr

`github.com/simon987/sist2`. `--exclude=<regex>` CLI flag (regex, not glob)
plus a `.sist2ignore` **file** using real gitignore syntax (`*.pdf` /
`!/important_files/*.pdf` -- negation supported). Confirmed: **ships zero
default exclusions** (absent from README/USAGE.md) -- sist2 leaves this
entirely to the user. Worth noting since it's the tool most similar to
Filearr's own scanning model (direct filesystem walk -> search index), and its
choice to ship nothing by default is a legitimate alternative to Filearr
shipping curated presets -- but Filearr's roadmap explicitly wants "one-click
preset bundles," so this brief treats sist2's minimalism as the null hypothesis
rather than the recommendation.

### 1.4 Spotlight (macOS)

Apple docs + Eclectic Light Company (independent macOS internals blog).
`.metadata_never_index` sentinel-file mechanism is **now broken on macOS
Sequoia+** -- noted as a cautionary tale for Filearr's own possible future
sentinel-file exclude ("don't index this folder" marker file): even Apple's
own long-standing mechanism has bitrotted. The only reliable user-facing lever
left is System Settings -> Spotlight -> Search Privacy (a GUI exclude list, not
a file convention) -- i.e. Apple moved this control into user-facing config,
which is directionally consistent with Filearr's own choice to make this a
UI/API-managed setting rather than a filesystem sentinel file.
`com.apple.metadata:com_apple_backup_excludeItem` (xattr) / `tmutil
addexclusion` / `CSBackupSetItemExcluded` API mark Time Machine exclusions --
an xattr-based approach, not applicable to Filearr's Linux-container/
SMB-mount deployment model. Default-excluded system paths on macOS:
`.DocumentRevisions-V100`, `.Spotlight-V100`, `.Trashes`, `.fseventsd`.

### 1.5 Syncthing `.stignore` (MPL-2.0)

`docs.syncthing.net/users/ignoring.html`. gitignore-style syntax with two
notable **extensions** beyond plain gitignore semantics that are directly
relevant to a preset-bundle design:
- **`(?i)`** -- case-insensitive prefix on a pattern (useful for
  `(?i)thumbs.db` matching `Thumbs.db`/`THUMBS.DB`/`thumbs.DB` uniformly across
  case-varying SMB mounts -- a real Filearr concern given cross-platform
  network shares).
- **`(?d)`** -- "delete if blocking a parent removal" prefix, purpose-built for
  exactly `.DS_Store`/`Thumbs.db`-class junk that would otherwise block a
  folder rename/delete sync. Not directly applicable to Filearr (read-only
  scanning, no write-back yet per invariant 6), but worth remembering if/when
  write-back ships in v2 -- Syncthing already solved "OS junk files interfere
  with directory operations."
- Syncthing uses **first-match-wins** semantics (not gitignore's
  last-match-wins) -- a real footgun to avoid replicating by accident; Filearr
  should pick one semantic and document it explicitly (recommendation below:
  match gitignore's last-match-wins, since that's what pathspec implements
  natively and what most users will expect from "gitignore-style").

### 1.6 github/gitignore (CC0-1.0 -- public domain, freely reusable verbatim)

Canonical, uncontroversial source for OS-junk patterns. Fetched raw files
(`raw.githubusercontent.com/github/gitignore/main/Global/*.gitignore` and
`Node.gitignore`):

**Windows.gitignore:**
```
Thumbs.db
Thumbs.db:encryptable
ehthumbs.db
ehthumbs_vista.db
*.stackdump
[Dd]esktop.ini
$RECYCLE.BIN/
*.cab
*.msi
*.msix
*.msm
*.msp
*.lnk
```

**macOS.gitignore** (abridged to the load-bearing entries; full file has ~34
lines):
```
.DS_Store
.AppleDouble
.LSOverride
Icon[\r]
._*
.DocumentRevisions-V100
.fseventsd
.Spotlight-V100
.TemporaryItems
.Trashes
.VolumeIcon.icns
.com.apple.timemachine.donotpresent
.AppleDB
.AppleDesktop
Network Trash Folder
Temporary Items
.apdisk
```
Note: `Icon[\r]` is a **literal filename ending in a carriage-return byte**
(`Icon\r`), a genuine Finder artifact -- a naive glob engine that doesn't treat
`\r` as a literal byte will silently fail to match this. Worth a unit test.

**Linux.gitignore:**
```
*~
.fuse_hidden*
.directory
.Trash-*
.nfs*
nohup.out
```

**Node.gitignore** (the relevant subset for "node_modules & build artifacts";
full upstream file also covers env files, editor dirs, etc. -- Filearr's
preset should pull only the filesystem/build-artifact lines, not e.g.
`.env*` which is a security-sensitive app convention, not junk):
```
node_modules/
jspm_packages/
dist/
.next
out/
.nuxt
.cache/
.parcel-cache
*.tsbuildinfo
.eslintcache
.npm
.yarn-integrity
npm-debug.log*
```

### 1.7 restic / Backrest

restic (BSD-2-Clause) + Backrest web UI (GPL-3.0, wraps restic). CLI:
`--exclude`, `--exclude-file` (newline-delimited pattern file, `#`-comments),
`--exclude-caches`, **`--exclude-if-present <name>`** (generalized sentinel-file
exclude -- same idea as Recoll's `nowalkfn`), `--exclude-larger-than`. Pattern
syntax = Go `filepath.Match` + `**` extension; supports `!` negation with the
caveat (documented explicitly by restic) that **you cannot re-include children
of an already-excluded directory** -- i.e. once a directory is pruned, negation
inside it is unreachable. This is an important semantic to nail down for
Filearr's own negation story: gitignore has the identical limitation (can't
un-ignore a file inside an ignored directory), so Filearr inherits it "for
free" by adopting gitignore semantics, and should document it rather than
promise arbitrary re-inclusion.

**CACHEDIR.TAG** (bford.info spec, public domain, ~1 KB spec, zero license
concerns): a directory containing a file literally named `CACHEDIR.TAG` whose
first 43 bytes are exactly `Signature: 8a477f597d28d172789f06886806bc55` is a
regenerable cache directory and should be skipped by backup/indexing tools.
Honored by GNU tar, restic, and many others. **Highest-ROI single item from
this whole survey**: zero maintenance burden (a ~10-line file-signature check),
automatically covers cache directories from tools Filearr's authors have never
heard of (any tool that adopts the convention in the future), and is a
strictly-additive, opt-out-able check (a directory can defeat it by simply not
creating the tag file).

### 1.8 czkawka (MIT core/CLI/GUI; GPL-3.0-only for newer Slint frontends)

`github.com/qarmin/czkawka`. `-e`/`--excluded-directories` (path-prefix, no
wildcard needed -- pure prune) vs `-E`/`--excluded-items` (wildcard, matches
anywhere, e.g. `*/.git */tmp* *Pulpit`). No hardcoded defaults shipped -- like
sist2, leaves exclusion entirely to the user. Reinforces the prune/name split
as a converged pattern independent of language/ecosystem (czkawka is Rust,
sist2 is C++/Go, Recoll is C++, Syncthing is Go).

### 1.9 Proposed preset bundles for Filearr

Legend: **[PRUNE]** = directory pattern, matched at directory-entry time to
stop descent (performance-critical -- must be checked before `scandir`
recurses); **[NAME]** = filename pattern, matched per-file/per-dir-entry
anywhere in the tree (checked but does not stop descent below it, since a
`NAME` match on a *file* has nothing below it, and a `NAME` match on a
*directory* that isn't also declared PRUNE still gets skipped without pruning
if the user wants finer control -- see engine recommendation in S2 for how this
is expressed as one gitignore-style pattern set rather than two APIs).

**Preset: `system_files`** -- cross-platform OS/filesystem bookkeeping:
```
$RECYCLE.BIN/          [PRUNE]
System Volume Information/  [PRUNE]
.Trashes/              [PRUNE]
.Trash-*/              [PRUNE]
.fseventsd/            [PRUNE]
.Spotlight-V100/       [PRUNE]
.DocumentRevisions-V100/  [PRUNE]
.TemporaryItems/       [PRUNE]
lost+found/            [PRUNE]
[Dd]esktop.ini         [NAME]
Thumbs.db              [NAME]
Thumbs.db:encryptable  [NAME]
ehthumbs.db            [NAME]
ehthumbs_vista.db      [NAME]
*.lnk                  [NAME]
.directory             [NAME]
.fuse_hidden*          [NAME]
.nfs*                  [NAME]
*~                     [NAME]
nohup.out              [NAME]
```

**Preset: `hidden_dotfiles`** -- `.*` **[NAME, matches dirs -> effectively
prunes]**. **Recommend shipping this preset OFF by default and require
explicit opt-in per library.** No surveyed tool defaults to a blanket dotfile
exclude (Recoll's default is a curated name list, not `.*`); a blanket rule
would also swallow Filearr's own T3 sidecar files that begin with `.`
(AppleDouble `._*` companions) which are meant to be *linked* via `sidecar_of`
and surfaced (hidden from default search, not silently dropped from the
catalog entirely -- those are different behaviors and this preset would
conflate them). Also note: today's `walk()` **already unconditionally skips
all dotfiles** at the code level (`entry.name.startswith(".")` in
`scan.py::walk`) -- see task T4-1 below, this needs to become the *default
state of this preset* (on by default today, implicitly) rather than removed
outright, to avoid a silent behavior change on upgrade.

**Preset: `caches_temp`**:
```
<CACHEDIR.TAG-validated directories>  [PRUNE, signature-checked, not name-based]
tmp/                   [PRUNE]
temp/                  [PRUNE]
.cache/                [PRUNE]
__pycache__/           [PRUNE]
.pytest_cache/         [PRUNE]
.tox/                  [PRUNE]
.direnv/               [PRUNE]
.ccls-cache/           [PRUNE]
.parcel-cache/         [PRUNE]
.nyc_output/           [PRUNE]
*.tmp                  [NAME]
*.cache                [NAME]
*.pyc                  [NAME]
#*                     [NAME]
```

**Preset: `node_modules_build`**:
```
node_modules/          [PRUNE]
jspm_packages/         [PRUNE]
bower_components/      [PRUNE]
.pnpm-store/           [PRUNE]
dist/                  [PRUNE]
.next/                 [PRUNE]
.nuxt/                 [PRUNE]
.svelte-kit/           [PRUNE]
.vite/                 [PRUNE]
target/                [PRUNE]   (Rust/Java/Maven convention)
.venv/                 [PRUNE]   (Python -- caveat: some users store real data
venv/                  [PRUNE]    under a dir literally named "venv"; document
                                   the risk in the UI when enabling this preset)
.eslintcache           [NAME]
*.tsbuildinfo           [NAME]
*.pyc                  [NAME]
npm-debug.log*         [NAME]
```

**Preset: `os_metadata`** -- narrower than `system_files`, the classic
gitignore Global set:
```
.DS_Store              [NAME]
._*                     [NAME]   (see caveat below)
Icon\r                  [NAME]   (literal CR byte in filename -- needs a
                                   unit test against the matching engine)
Thumbs.db               [NAME]
desktop.ini              [NAME]
```
Caveat: `._*` (AppleDouble resource-fork companion files) directly overlaps
Filearr's T3 sidecar-detection domain (`sidecar.classify()`). Recommend
`sidecar.classify()` continues to claim these files as sidecars (parent-linked,
hidden-but-cataloged) rather than this preset hard-excluding them outright --
i.e. sidecar classification should run **before** preset exclusion is applied,
or presets should never fire on paths sidecar detection already claims. This
is an ordering decision the implementation must get right (see S6 open
questions).

### 1.10 Default search locations per platform (roadmap sub-bullet)

Not deeply covered by the tool survey (this is an agent-setup UX concern more
than an indexing-exclusion one), but the precedent is simple and
well-established: ship a small **per-platform default root list** at agent
enrollment time (v3), editable centrally afterward:
- Windows: `%USERPROFILE%\Desktop`, `%USERPROFILE%\Documents`,
  `%USERPROFILE%\Downloads`, `%USERPROFILE%\Pictures`
- macOS: `~/Desktop`, `~/Documents`, `~/Downloads`, `~/Pictures`
- Linux: XDG user dirs (`~/.config/user-dirs.dirs` -- `xdg-user-dir DESKTOP`
  etc.) with a hardcoded `~/Desktop`, `~/Documents`, `~/Downloads` fallback
  when XDG isn't configured (common on minimal/server distros).
This is a v3 (agent) concern -- v1/v2 Filearr has no per-machine agent yet, so
this only needs a code-constant table (analogous to `media_types.py`) ready to
be surfaced in the agent-enrollment wizard when that ships; no schema work
needed now.

---

## 2. Glob/ignore-pattern engine recommendation for Python

Compared three options for matching `include_globs`/`exclude_globs` (now
becoming named preset-bundle pattern sets) against `rel_path` during the scan
walk, with a directory-pruning requirement for performance at scale:

**pathspec** (PyPI `pathspec`, MPL-2.0). Latest 1.1.1 (2026-04-26/27 per
research), actively maintained (last commit 2026-06-03, only 6 open issues).
The `'gitwildmatch'` string-factory API is deprecated in favor of
`GitIgnoreSpec.from_lines()`, which correctly implements gitignore's
last-match-wins negation precedence (regression-tested against historical
negation bugs). Directory-only patterns (trailing `/`) work via
`pathspec.util.append_dir_sep()`. **Gap**: no built-in single-pass
walk-and-prune -- its convenience tree helpers fully traverse then filter
after the fact, so Filearr must call `spec.match_file()` itself inside a
manual `scandir(topdown=True)`-style loop, checking each directory entry
*before* recursing to get the pruning benefit. Pluggable regex backends
(`simple`/`hyperscan`/`re2`); `re2` backend is meaningfully faster once
combined pattern counts exceed roughly two dozen (relevant once multiple
preset bundles stack on one library).

**wcmatch** (PyPI `wcmatch`, MIT). Latest 10.2.1 (2026-07-02, very actively
maintained, 1 open issue). Correction to an initial assumption: **wcmatch has
no gitignore-mode flag at all** (verified by enumerating its flags -- there is
no `GITIGNORE` constant). Its `WcMatch` class does implement true single-pass
directory pruning natively ("cannot add an exception for the child of an
excluded folder" -- same restic/gitignore limitation, confirmed independently),
but using its own Bash-glob-derived pattern dialect, not gitignore syntax -- a
Syncthing/gitignore-style `!re-include` rule would need hand-translation to
wcmatch's dialect, which is exactly the negation/preset-bundle interaction
the roadmap explicitly wants to support cleanly.

**Python 3.13 stdlib** (`fnmatch`, `pathlib.PurePath.full_match()`,
`glob.translate()`). Confirmed both `Path.full_match()` and `glob.translate()`
are new in 3.13. `fnmatch.translate()` is `functools.lru_cache`-backed
(`maxsize=32768`). Confirmed gap: stdlib has **no whole-pattern-set negation
and no gitignore precedence** -- every entry point matches one pattern at a
time; negation/precedence would need to be hand-rolled in application code
(which is, in effect, reimplementing pathspec badly).

### Recommendation: **pathspec** (`>=1.1.0`, MPL-2.0)

Use `pathspec.GitIgnoreSpec` as a pure matching oracle called from Filearr's
own `scan.py::walk()` loop: `spec.match_file(append_dir_sep(dirpath))` per
directory (to prune before `scandir` recurses -- replacing today's ad-hoc
`entry.name.startswith(".")` check and the current post-hoc `fnmatch.fnmatch`
calls), and `spec.match_file(rel)` per file otherwise. Justification:
gitignore-fidelity and correct negation are the two properties Filearr's
roadmap explicitly asks for ("preset bundle X, but re-include this one file")
and pathspec is the only surveyed option that gets both right natively out of
the box; replicating wcmatch's out-of-the-box pruning convenience on top of
pathspec is a small, contained ~10-20 line addition to the existing `walk()`
function, versus reimplementing gitignore negation semantics on top of
wcmatch's non-gitignore dialect (much larger, correctness-risky effort). MPL-2.0
is an unconditionally safe dependency for an AGPL-3.0-or-later project (weak
copyleft, file-level, no interaction with AGPL's network-use clause). Revisit
`backend='re2'` if the number of active patterns (built-in presets + per-library
overrides, stacked) grows large enough to show up in scan-throughput profiling;
not needed at today's baseline (~24 files/s over SMB -- the walk is IO-bound,
not CPU-bound, at current scale).

Migration note: this changes matching semantics from today's raw
`fnmatch.fnmatch` (no negation, no directory-prune, matched twice -- once for
exclude, once for include) to gitignore precedence (last-match-wins across a
single ordered pattern list). Existing `include_globs`/`exclude_globs` values
already stored in the DB are still valid gitignore patterns (fnmatch and
gitignore glob syntax mostly overlap for simple patterns like `*.mkv`), but
any library relying on fnmatch's simpler two-list-AND semantics
(`exclude wins outright, else include must match`) should be re-verified after
the swap -- this is a **behavior-changing migration**, not a pure refactor, and
needs its own test coverage (see T4-2 below).

---

## 3. Hot-folder scheduling: prior art and extension of T5

### 3.1 The *arr suite (Radarr/Sonarr/Lidarr/Readarr)

Multiple root folders are supported (Settings -> Media Management), but
**scan/refresh cadence is global to the instance, not per-root-folder**
(wiki.servarr.com/sonarr/settings, /sonarr/system). "Refresh Series"/RSS Sync
run on one shared schedule; RSS Sync interval is configurable (10-120 min,
0=disabled) but as a single global value. Direct evidence of user demand for
finer granularity: **Sonarr/Sonarr#1592** ("Option to disable periodic disk
rescan") -- users on rclone/GDrive/NAS complained the fixed 12-hour global
rescan wakes disks / hits cloud API rate limits and explicitly asked for
per-schedule/user-defined intervals; it shipped only as a global on/off
advanced toggle, never per-root granularity (one user resorted to a manual
SQLite `UPDATE ScheduledTasks SET Interval=...` hack -- a telling signal that
the demand is real but unmet upstream). Related-but-not-matching:
Sonarr#6658 (multi-root-per-series, not cadence), Sonarr#5926 (per-root
recycle bin), Radarr#5543 (unmonitored-movie refresh scoping). **No *arr
issue grants a distinct scan interval per root folder** -- this is confirmed
white space Filearr would be filling, not a solved problem to copy.

### 3.2 Paperless-ngx

Single consume dir (`PAPERLESS_CONSUMPTION_DIR`); multi-consume-folder support
is an open, unresolved feature request (paperless-ngx#6430 discussion).
Polling is one global knob: `PAPERLESS_CONSUMER_POLLING` (seconds; 0=disabled,
uses inotify instead), `PAPERLESS_CONSUMER_POLLING_RETRY_COUNT`,
`PAPERLESS_CONSUMER_POLLING_DELAY` (docs.paperless-ngx.com/configuration). Its
**"Workflows"** feature is the closest thing to per-folder granularity, but it
governs processing *rules* (tag/owner/correspondent assignment) via a
file-path glob filter on the "Consumption Started" trigger -- it has **zero
effect on scan/poll frequency**, and only fires for consume-folder imports.

### 3.3 FileFlows (closed-source -- cited for pattern only, never as a dependency)

Per public docs (fileflows.com/docs): each Library object owns a folder path,
an assigned Flow (processing pipeline reference), a **Schedule** (a
time-window control on when the scanner may run), a scan-interval fallback for
missed filesystem events, a priority, and a hold-off delay before processing
new files. This is a genuine per-library schedule+pipeline pairing -- the
closest commercial analog to what the roadmap asks for -- but note it is
**per-library, not per-subfolder-within-a-library**, same granularity Filearr
already has via `scan_cron`/`watch_mode`. Cited strictly for the
"schedule lives on the same object as the thing it governs" design shape;
Tdarr (also closed-source) follows the same library/flow/schedule pattern.

### 3.4 Immich

External-library scan interval is **global per server** (Administration ->
Settings -> External Library, one cron expression applied uniformly to every
library). Each library has its own import paths + exclusion globs (e.g.
`**/Raw/**`) but not its own cadence. Immich's watcher is explicitly
documented as unreliable over network drives -- "If your photos are on a
network drive, automatic file watching likely won't work... you will have to
rely on a periodic library refresh" (docs.immich.app/features/libraries) --
nearly identical to Filearr's own local-watch/network-poll split (T5), but
Immich stops at one global interval with no per-folder override, same gap as
the *arrs.

### 3.5 Beets, rclone+systemd community patterns

Beets has no native watch/daemon; the community bolts on inotify wrappers
(`drop2beets`) or ad hoc `inotifywait`-triggered `beet import -i` scripts.
rclone-mount users commonly hand-roll one **systemd service** (the mount)
paired with one **systemd timer** (periodic rescan) *per remote*
(`hekmon/rclonemount`, `nilreml/rclone-mount-systemd`) -- i.e., people already
retrofit per-mount scheduling via OS-level timers precisely because no
surveyed tool supports it natively. This is corroborating evidence that
Filearr shipping native per-path scheduling is solving a real, currently
DIY-only problem -- worth stating in release notes / feature framing.

**Bottom line: no surveyed tool natively supports a distinct scan/watch
frequency for a sub-folder within one library/root.** Filearr's planned
hot-folder scheduling is genuinely ahead of this prior art, not a "catch up"
feature -- which raises the bar on getting the data model right the first
time, since there's no reference schema to copy.

### 3.6 Extending T5 cleanly: per-path rows evaluated by the same tick

T5's standing constraint is unchanged: **Procrastinate periodic tasks are
import-time static**, so per-path scheduling cannot register N new periodic
tasks at runtime -- it must be additional rows evaluated by the *same* static
tick, exactly as T5 already does for per-library `scan_cron`.

**Recommended shape: a new `scan_paths` table, not a schema explosion on
`libraries`.**

```
CREATE TABLE scan_paths (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    library_id UUID NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,          -- path relative to library root_path;
                                      -- '' (empty string) = the library root itself
    scan_cron TEXT,                   -- cronsim expression; NULL = inherit library's
    watch_mode BOOLEAN,               -- NULL = inherit library's watch_mode
    enabled BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (library_id, rel_path)
);
CREATE INDEX ix_scan_paths_library_id ON scan_paths(library_id);
```

Rationale for a **separate table** over adding columns to `libraries` or a
JSONB blob:
- Matches the existing `ScanRun`/`Item` pattern (one row per governed entity,
  FK to `library_id`, indexed) rather than introducing a JSONB config bag for
  something that needs per-row uniqueness (`UNIQUE(library_id, rel_path)`),
  per-row `enabled` toggling, and straightforward API CRUD (`GET/POST/PATCH/
  DELETE /libraries/{id}/scan-paths`) -- all much more natural as rows than as
  array/JSONB manipulation.
- `rel_path = ''` cleanly represents "override for the whole library" without
  a sentinel or a separate "applies to root" boolean, and reuses the exact
  identity convention already in place for `items.rel_path` (invariant 3).
- NULL-inherits-from-library on both `scan_cron` and `watch_mode` means a
  `scan_paths` row is purely an **override**, not a full duplicate config --
  consistent with T7's `hash_full_max_bytes` NULL-inherits-global pattern
  already in the codebase (`libraries.hash_full_max_bytes: int | None`, "NULL
  -> fall back to the global setting").

**Evaluation: same static tick, longest-prefix-wins resolution, not a second
tick task.** Extend `_defer_due_scans`/`schedule_scans` (both in
`worker.py`) to, per enabled library, load its `scan_paths` rows once, and for
each due `scan_paths` row defer a scan scoped to `rel_path` rather than (or in
addition to) the whole-library scan. This requires `scan_library` /
`_scan_body` to accept an optional `rel_path` scope so `walk()` starts at
`os.path.join(root_path, rel_path)` instead of `root_path` -- a **targeted,
constrained walk**, not a new scan pipeline (preserves T5's explicit "one scan
code path" decision: move detection, sidecar association, and tombstoning
still need whole-library-relative-path context for `existing` diffing, so a
scoped scan should still diff against the *library's* full `existing` item map
filtered to items under `rel_path`, not re-derive identity -- this needs care,
flagged as an open question in S6).

Conflict resolution when a path falls under **multiple** `scan_paths` rows
(e.g. a `scan_paths` row for `Downloads/` and another for
`Downloads/Incoming/`): use **longest-`rel_path`-prefix-wins**, the same
resolution rule `nginx` `location` blocks and Syncthing's own path-based rules
use -- pick the most specific override, fall back through less specific ones,
fall back to the library-level `scan_cron`/`watch_mode` if no `scan_paths` row
covers the path at all. This needs to be a pure, unit-testable function
(mirroring `schedule.cron_is_due`'s existing testability discipline) -- e.g.
`schedule.resolve_scan_path(scan_paths: list[ScanPath], rel_path: str) ->
ScanPath | None`.

**Watch-mode extension**: `WatchSupervisor._desired()` currently returns one
`(library_id -> root_path)` map. Extend it to return one watcher **per
distinct effective `watch_mode=True` path** (library root, or any
`scan_paths` row that overrides `watch_mode` to `true`/`false` independently
of the library default) -- i.e. potentially multiple `watchfiles.awatch()`
tasks per library, each scoped to its own subtree, still refused for network
roots by the same `is_network_path` guard (a `scan_paths` row cannot re-enable
watch on a network mount any more than the library-level toggle can -- the
guard must be re-checked per resolved path, not only at the library level,
since a library's root could be local while a specific `scan_paths` subfolder
is itself a separate network bind-mount point in exotic setups. This is an
edge case worth a defensive check even if rare).

This design is additive: libraries with zero `scan_paths` rows behave exactly
as today (T5 unchanged), and the static-tick constraint is honored -- no new
periodic task registration, just a richer query inside the existing tick.

---

## 4. Central preset-bundle data model: precedent and recommendation

### 4.1 osquery config packs

A **pack** is a named entry under top-level `packs` in osquery's JSON config
(osquery.readthedocs.io/en/stable/deployment/configuration/):

```json
{
  "packs": {
    "internal_stuff": {
      "discovery": ["SELECT pid FROM processes WHERE name = 'ldap';"],
      "platform": "linux",
      "version": "1.5.2",
      "queries": {
        "active_directory": {
          "query": "SELECT * FROM ad_config;",
          "interval": "1200",
          "description": "Check each user's active directory cached settings."
        }
      }
    }
  }
}
```
Fields: `queries` (map), `discovery` (SQL probes gating pack activation, checked
every `pack_refresh_interval`, default 60 min), `platform`/`version`/`shard`
inherited pack-wide. Versioning is informal (plain JSON files on disk, gated
only by a minimum-osquery-`version` string per query/pack). Bundled "discovery
packs" ship in the osquery GitHub repo's `packs/` directory as curated,
release-versioned files; custom packs are user files referenced by path or
glob. This maps directly to Filearr's existing "code constants for shipped
defaults" pattern (`media_types.py::EXT_MAP`).

### 4.2 Fleet's policies (built on osquery)

Fleet (fleetdm.com) layers "policies" (pass/fail SQL, e.g. `SELECT 1 FROM
...`) on top of osquery's raw query/pack mechanism. Confirmed policy object
shape implies a DB-backed, attributed, revisable model: `id`, `name`, `query`,
`description`, `author_id`/`author_name`/`author_email`, `resolution`,
`passing_host_count`/`failing_host_count`. Policies are authorable as YAML and
applied via `fleetctl apply` (GitOps workflow). **Config push to agents is
fixed-interval polling, not ETag/webhook-based**: `fleetd` pulls merged config
via `GetClientConfig`; the confirmed setting is
`osquery.policy_update_interval` (default 1h, env
`FLEET_OSQUERY_POLICY_UPDATE_INTERVAL`), sibling
`osquery_detail_update_interval` for host-detail refresh
(fleetdm.com/docs/configuration/fleet-server-configuration). This is the
concrete precedent for how a *future* Filearr v3 agent would pull preset-bundle
policy: fixed polling interval + a version/revision comparison, not push/
webhook infrastructure -- simpler and sufficient at Fleet's own scale.

*(Gap noted honestly: the exact Fleet MySQL migration/column-type file for the
`policies` table could not be retrieved during this research session -- the
field list above is reconstructed from Fleet's public docs and API responses,
not a verified schema dump. If exact Fleet column types are needed before
implementation, do a follow-up direct repo check.)*

### 4.3 osquery `file_paths` / `exclude_paths` -- the most directly relevant precedent

This is the strongest single precedent found, because it is *already solving
Filearr's exact problem* (named, groupable include/exclude glob categories)
inside osquery's own File Integrity Monitoring config
(osquery.readthedocs.io/en/stable/deployment/file-integrity-monitoring/):

```json
{
  "file_paths": {
    "homes": ["/root/.ssh/%%", "/home/%/.ssh/%%"],
    "etc": ["/etc/%%"],
    "tmp": ["/tmp/%%"]
  },
  "exclude_paths": {
    "homes": ["/home/not_to_monitor/.ssh/%%"],
    "tmp": ["/tmp/too_many_events/"]
  }
}
```

Key semantics (from the docs, quoted): arbitrary category names under
`exclude_paths` are **rejected silently** unless that exact category name also
exists under `file_paths` -- i.e. **the category name is the join key** between
an inclusion set and its corresponding exclusion subtractions. Wildcard
grammar: `%` = one path segment, `%%` = recursive, no mid-path recursive
matching. `file_accesses` is a flat array of category names additionally
opted into read-event monitoring.

This **named-category-as-join-key** shape is exactly the missing piece in
Filearr's current flat `include_globs`/`exclude_globs` arrays: today there is
no way to say "here is a named, independently-toggleable group of patterns"
(a preset bundle) -- only two undifferentiated lists. Naming the category is
what turns a flat array into a toggleable UI checkbox ("system files: on",
"node_modules: off") and, later, into a distributable policy unit (a v3 agent
pulls "the node_modules_build bundle, version 4" as one atomic thing, exactly
as an osquery pack or a Fleet policy is one atomic thing).

### 4.4 Recommendation: hybrid, staged, no premature schema

Filearr already runs the exact hybrid shape this precedent validates: code
constants for shipped defaults (`media_types.py::EXT_MAP` ~= osquery's bundled
`packs/` directory) plus per-row Postgres array overrides
(`libraries.include_globs`/`exclude_globs` ~= osquery's per-host custom pack
config / Fleet's DB-backed policies). The recommendation is to **extend this
existing shape incrementally, not replace it**:

**Stage A (now -- this is the v2 scope for roadmap item 4, size M, do this):**
Add a code-constant module `backend/filearr/presets.py` (sibling to
`media_types.py`), structured as a dict of named bundles:
```python
PRESET_BUNDLES: dict[str, PresetBundle] = {
    "system_files": PresetBundle(
        label="System files",
        exclude=["$RECYCLE.BIN/", "System Volume Information/", ...],
    ),
    "hidden_dotfiles": PresetBundle(
        label="Hidden/dotfiles", exclude=[".*"], default_enabled=True,
        # default_enabled=True preserves today's unconditional walk()-level
        # dotfile skip as this preset's shipped default, so enabling the
        # preset system is not a silent behavior change on upgrade.
    ),
    "caches_temp": PresetBundle(...),
    "node_modules_build": PresetBundle(...),
    "os_metadata": PresetBundle(...),
}
```
Add a **new `libraries.enabled_presets` column** (`ARRAY(Text)`,
`server_default='{}'`, same style as `enabled_types`) storing the *names* of
enabled bundles. At scan time, the effective exclude pattern list =
`union(PRESET_BUNDLES[name].exclude for name in enabled_presets) +
library.exclude_globs` (bundles first, then per-library custom excludes,
combined into one `pathspec.GitIgnoreSpec` per the S2 recommendation -- this
also gives per-library custom `include_globs` entries the ability to
re-include a single file a preset would otherwise exclude, satisfying the
roadmap's explicit "preset bundle X, but re-include this file" requirement via
gitignore negation, not a bespoke override mechanism).

Also extend `media_types.py`'s coarse per-`MediaType` toggle with a
**file-type preset** layer for arbitrary extension groups (the roadmap's
"file-type presets... centrally managed" sub-bullet): a second code-constant
dict, e.g. `EXTENSION_GROUPS = {"raw_photos": ["cr2","cr3","nef","arw","dng",
"raf"], "office_docs": ["doc","docx","odt","rtf"], ...}`, with a
`libraries.enabled_extension_groups` column (`ARRAY(Text)`) analogous to
`enabled_presets`. This extends, rather than replaces, `enabled_types` (which
stays as the coarse `MediaType`-bucket toggle); extension groups are a finer
grain filter *within* an enabled `MediaType`, resolved the same way
(`effective enabled extensions = enabled_types buckets intersect enabled_extension_groups,
when any extension group is set for that type; else all extensions in the
bucket`) -- exact resolution semantics need a short design note, flagged in S6.

This mirrors osquery's bundled-`packs/`-directory-vs-custom-config split and
requires **no new table** -- a genuine "don't over-build" outcome, since
Filearr has no agents yet and no multi-tenant reason to version bundles
centrally.

**Stage B (bridge, size M, do only once bundles need to be user-editable
via UI/API rather than code-constant, likely a v2-late or v3-early trigger,
not now):** Promote `PRESET_BUNDLES` from a pure code constant to a DB-backed,
seed-from-code table:
```sql
CREATE TABLE preset_bundles (
    id UUID PRIMARY KEY DEFAULT uuidv7(),
    name TEXT UNIQUE NOT NULL,       -- e.g. 'system_files' (join key, osquery-style)
    label TEXT NOT NULL,
    kind TEXT NOT NULL,               -- 'glob' | 'extension_group'
    include_globs TEXT[] DEFAULT '{}',
    exclude_globs TEXT[] DEFAULT '{}',
    extensions TEXT[] DEFAULT '{}',   -- populated when kind='extension_group'
    is_builtin BOOLEAN NOT NULL DEFAULT true,  -- seeded from code; false = user-created
    version INTEGER NOT NULL DEFAULT 1,         -- bump on any edit (Fleet-style)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```
`libraries.enabled_presets`/`enabled_extension_groups` then reference
`preset_bundles.name` (still just text arrays -- no join table needed for a
simple enable/disable-by-name relationship; a join table would only be
justified if per-library *bundle customization* were needed, which is not in
scope). `is_builtin` rows are re-seeded/reconciled from the code constants on
startup (matching init_db's existing idempotent-bootstrap discipline) so
built-ins stay a single source of truth even after DB promotion, and
`is_builtin=false` rows are pure user-created bundles via API.

**Stage C (v3, agent distribution -- not now, but the version column above is
the load-bearing piece that makes this a non-breaking addition later):** an
agent-facing pull endpoint returns `{name, kind, globs/extensions, version}`
per bundle; a v3 agent stores last-applied version per bundle name and
re-pulls on mismatch, polled at a fixed interval -- directly mirroring Fleet's
`policy_update_interval` polling model (no ETag/webhook machinery needed,
consistent with "don't over-build" -- Fleet itself doesn't use anything fancier
than interval polling for this). Because Stage B already has the `version`
column, this stage requires **zero migration**, only a new read-only API route
and an agent-side poll loop (which doesn't exist yet regardless -- agents are
v3 scope per the roadmap's own sequencing).

This staging directly avoids two failure modes: building a fully versioned,
distributed policy system now when there are no agents to distribute *to*
(over-building), and leaving presets as unnamed flat arrays indefinitely,
which cannot be grouped, toggled, or later distributed as named units without
a breaking migration (under-building). The one thing worth doing *early*
(Stage A) is naming the categories, because that's the cheap, structurally
required part validated by osquery's own `file_paths`/`exclude_paths` design.

---

## 5. API surface (v2 scope, aligned with Stage A)

Extends `backend/filearr/api/libraries.py`'s existing PATCH-based pattern
(`LibraryUpdate`, built from `model_fields_set` per the T7 null-clear fix --
reuse this discipline for the new array fields so an explicit
`{"enabled_presets": []}` reliably clears to "no presets" rather than being
silently dropped).

- `GET /api/presets` (read scope) -- list `PRESET_BUNDLES` (Stage A: code
  constant, serialized) or `preset_bundles` rows (Stage B), including bundle
  name/label/kind/pattern-count, for the UI's preset-toggle checkboxes.
- `GET /api/presets/{name}` (read scope) -- full pattern list for one bundle
  (an "inspect before enabling" affordance -- users should be able to see
  exactly what `system_files` excludes before toggling it on).
- `PATCH /libraries/{id}` -- extend existing `LibraryUpdate` schema with
  `enabled_presets: list[str] | None` and `enabled_extension_groups: list[str]
  | None`; validate each name against `PRESET_BUNDLES`/`EXTENSION_GROUPS` keys
  at write time (422 on unknown name, mirroring today's `_validate_schedule_
  fields` pattern for `scan_cron`).
- `GET /libraries/{id}/scan-paths`, `POST /libraries/{id}/scan-paths`,
  `PATCH /libraries/{id}/scan-paths/{scan_path_id}`, `DELETE
  /libraries/{id}/scan-paths/{scan_path_id}` (write/admin scope for
  mutations) -- CRUD for the T5-extension `scan_paths` table (S3.6). `scan_cron`
  validated with the existing `schedule.validate_cron`; `watch_mode=true`
  re-validated against `is_network_path` for the **resolved absolute path**
  (`root_path` + `rel_path`), not just the library root.
- (Stage B only) `POST /api/presets` / `PATCH /api/presets/{name}` (admin
  scope) for user-created/edited bundles; `is_builtin=true` bundles are
  read-only via this route (editing a builtin should fork a copy, not mutate
  the shipped default in place -- protects the "reconciled from code on
  startup" invariant in S4.4 Stage B).

---

## 6. UI notes (Svelte 5, `frontend/src/lib/AdminPage.svelte`)

- Preset bundles surface as a **checkbox group** per library (one click per
  bundle, matching the roadmap's explicit "toggled with one click" language),
  each with an expandable "show patterns" disclosure (calling `GET
  /api/presets/{name}`) so users aren't enabling an opaque black box --
  directly addresses the Everything/Recoll-style trust concern where users
  want to see what a "system files" exclude actually touches before trusting
  it on a real media library.
- `hidden_dotfiles` preset should render with an explicit inline caveat
  (e.g. "may hide some sidecar/companion files -- see docs") given the T3
  sidecar-overlap caveat from S1.9.
- Extension-group presets get a similar checkbox layout, scoped/nested under
  each `MediaType`'s existing enable toggle in the current Admin UI (extension
  groups only make sense as a refinement of an already-enabled type).
- `scan_paths` (hot-folder overrides) get a new sub-table under each library's
  admin card: path (relative, with a folder picker/autocomplete against a
  `GET /libraries/{id}/browse?path=` style endpoint if one exists, else free
  text validated server-side against existing items' `rel_path` prefixes),
  cron override, watch-mode override (tri-state: inherit / on / off), enabled
  toggle. Reuse the existing `scan_cron` validation UX from the library-level
  form (client-side cronsim-equivalent hint text already exists per T5).

---

## 7. Task breakdown (T-numbered per project convention, sized, with accept criteria)

**T4-1 -- Preset bundle module + `enabled_presets` column (size M)**
Add `backend/filearr/presets.py` (`PRESET_BUNDLES` dict per S1.9/S4.4 Stage A,
including `hidden_dotfiles` with `default_enabled=True` to preserve today's
unconditional dotfile skip without a silent behavior change). Add
`libraries.enabled_presets` (`ARRAY(Text)`, migration). Wire effective-exclude
resolution into `scan.py::walk()`. Replace `walk()`'s raw `fnmatch.fnmatch`
calls and the hard-coded `entry.name.startswith(".")` check with a single
`pathspec.GitIgnoreSpec` built from `union(preset excludes) + library.exclude_
globs`, checked per-directory-entry for pruning and per-file otherwise;
`include_globs` continues to work as a re-inclusion mechanism via gitignore
negation semantics.
*Accept:* a library with `enabled_presets=["node_modules_build"]` never
descends into a `node_modules/` directory (verify via a scan on a fixture tree
with a deeply-nested `node_modules/` containing enough files that a
non-pruning implementation would be measurably slower -- assert on directory-
entry count or walk timing, not just final item count, to actually prove
pruning happened); disabling all presets reproduces at least the same file set
as today's scan (regression: today's implicit all-dotfiles-skipped behavior is
preserved by `hidden_dotfiles` defaulting on, not silently dropped).

**T4-2 -- pathspec migration correctness tests (size S)**
Dedicated test module asserting: gitignore negation re-includes a file inside
an otherwise-excluded bundle (`exclude=["*.log"]`, `include=["!important.log"]`
equivalent -- confirm the exact API shape once T4-1 lands, likely a single
combined pattern list rather than two separate include/exclude lists, since
gitignore semantics are inherently one ordered list with `!`-negation, not two
ANDed lists -- this is the headline semantic change from today's fnmatch
approach and needs explicit before/after test coverage); `Icon\r` (literal CR
byte) matches; case-sensitivity behavior is documented and tested (gitignore
patterns are case-sensitive by default on Linux -- note this explicitly since
Filearr's SMB-mounted libraries may have case-varying filenames, a real
cross-platform gotcha this brief flags but does not fully resolve -- see open
questions).
*Accept:* all restated Windows/macOS/Linux gitignore patterns from S1.6 match
their intended files/dirs in a fixture tree; a negation pattern demonstrably
re-includes a file a preset would otherwise exclude.

**T4-3 -- Extension-group presets (size S)**
`EXTENSION_GROUPS` code constant (S4.4), `libraries.enabled_extension_groups`
column, resolution layered under existing `enabled_types`/`media_types.detect`.
API validation for unknown group names (422).
*Accept:* enabling `office_docs` on a `document`-enabled library indexes only
`.doc/.docx/.odt/.rtf` from that type bucket, not all `document`-mapped
extensions; disabling the group (or leaving no group set) falls back to "all
extensions in the enabled type," matching today's behavior.

**T4-4 -- CACHEDIR.TAG detection (size S)**
Directory-prune check reading the first 43 bytes of a candidate `CACHEDIR.TAG`
file and comparing against the exact bford.info signature before pruning (not
just filename presence -- the signature check is what makes this safe against
a directory that merely happens to contain a file with that name).
*Accept:* a fixture directory with a valid `CACHEDIR.TAG` is pruned entirely;
a directory with a file named `CACHEDIR.TAG` but wrong/missing signature
content is NOT pruned (false-positive guard).

**T4-5 -- Preset bundle API + Admin UI (size M)**
`GET /api/presets`, `GET /api/presets/{name}`, `PATCH /libraries/{id}`
extension for `enabled_presets`/`enabled_extension_groups`. AdminPage checkbox
groups with pattern-disclosure per S6.
*Accept:* toggling a preset checkbox in the UI round-trips through a real scan
and visibly changes which files are indexed (manual/E2E verification, mirroring
T7's "library setting visibly changes scan IO profile" acceptance style).

**T4-6 -- `scan_paths` table + hot-folder scheduling (size L)**
New table (S3.6), migration, `resolve_scan_path` pure/unit-testable function
(longest-prefix-wins), extension of `_defer_due_scans`/`schedule_scans` to
evaluate per-path overrides on the same static tick, `scan_library`/
`_scan_body` accepting an optional `rel_path` scope for a targeted walk while
still diffing against the library's full existing-item map (needs the
whole-library-context care noted in S3.6), `WatchSupervisor` extended to run
one watcher per distinct effective watch-enabled path (still refusing network
roots per-resolved-path, not only per-library).
*Accept:* a `scan_paths` row `{rel_path: "Downloads", scan_cron: "* * * * *"}`
on a library whose own `scan_cron` is nightly causes `Downloads/` to be
re-walked every minute while the rest of the library follows the nightly
schedule; a `scan_paths` row cannot enable `watch_mode` on a path that resolves
to a network mount even if the library root is local (defensive re-check);
removing all `scan_paths` rows for a library reproduces T5's exact original
behavior (regression coverage against T5's existing test suite).

**T4-7 -- Preset bundle DB promotion (Stage B) (size M, deferred -- not v2, do
only when user-editable bundles are actually requested)**
`preset_bundles` table, startup reconciliation of `is_builtin` rows from code
constants, `POST/PATCH /api/presets` for user-created bundles, fork-not-mutate
semantics for builtins.
*Accept:* a user-created bundle survives a server restart and a code-constant
change to the builtins does not silently alter a user's custom bundle of the
same conceptual purpose (name collision handling needs its own test).

**T4-8 -- Default search locations per platform (v3-adjacent, size S, deferred
to agent-enrollment work)**
Code-constant per-platform default root table (S1.10), surfaced only once an
agent-enrollment wizard exists (roadmap S1) -- no schema work needed until then.
*Accept:* deferred; no accept criteria until agent enrollment ships.

---

## 8. Open questions

1. **Sidecar-vs-preset ordering.** Should `sidecar.classify()` run before or
   after preset-exclusion matching in `walk()`? Recommendation leans "sidecar
   classification wins" (a file already claimed as a sidecar should never be
   silently dropped by e.g. an `os_metadata` preset matching `._*`), but this
   needs an explicit decision and test, not an implicit ordering artifact of
   whichever code happens to run first.
2. **Case sensitivity on SMB/network mounts.** gitignore patterns are
   case-sensitive by default; Windows/SMB filesystems are commonly
   case-insensitive/case-preserving. A pattern like `Thumbs.db` (case-exact)
   may fail to match `thumbs.db` on some real-world network shares. Syncthing's
   `(?i)` prefix extension (S1.5) is the precedent for solving this, but
   pathspec's `GitIgnoreSpec` doesn't natively support a per-pattern
   case-insensitive flag -- needs a decision: normalize known OS-junk patterns
   to a case-insensitive form at the application layer (e.g. bracket-expand
   `[Tt]humbs.db`, matching what the Windows.gitignore template already does
   for `[Dd]esktop.ini`), or accept the gap and document it.
3. **Scoped scan diff correctness (T4-6).** A `scan_paths`-scoped scan walks
   only a subtree, but move/rename detection (T2) and sidecar association (T3)
   were both explicitly designed around whole-library context. Does a scoped
   scan skip move-detection entirely for files under its subtree (falling back
   to tombstone+recreate, same as today's `move_ambiguous` fallback), or does
   it need access to the full library's `existing` map read-only for matching
   purposes while only writing/diffing within its subtree? This needs a design
   decision before T4-6 implementation, not just an implementation detail.
4. **Preset bundle versioning triggers for Stage B.** What concrete signal
   should trigger promoting `PRESET_BUNDLES` from code constant to DB table
   (Stage B)? Candidates: first real user request for a custom (non-builtin)
   bundle, or the start of v3 agent-enrollment work (since agents need
   *something* to poll, and Stage B's `version` column is the prerequisite).
   Recommend treating "user wants to create/edit a bundle via UI" as the
   trigger, not a fixed roadmap date.
5. **Extension-group and MediaType interaction precision.** S4.4's proposed
   resolution rule ("group set for the type -> only those extensions; no group
   set -> all extensions in the bucket") needs to be nailed down against a
   concrete UI mock before implementation -- in particular, can multiple
   extension groups be enabled simultaneously for one `MediaType` (e.g. both
   `raw_photos` and `jpeg_family` under `image`), and is that a union or does
   enabling any group implicitly disable "all extensions" mode entirely? This
   is a small but real product decision, not just an engineering one.
6. **`hidden_dotfiles` default-on migration path.** Making today's hard-coded
   dotfile skip into an explicit, technically-disable-able preset is a
   deliberate reduction of a previously-unconditional guarantee. Should
   disabling `hidden_dotfiles` on an existing library require a confirmation
   step (since it changes catalog contents, potentially surfacing files users
   never expected to see indexed), or is a plain toggle sufficient? Leaning
   toward a mild UI warning given "integrity" outranks "compatibility" in the
   project's stated priority order.

---

## Sources cited (URLs, by section)

- S1.1: voidtools.com/support/everything/options/, voidtools.com/support/everything/searching/
- S1.2: recoll.org/usermanual (skippedPaths/skippedNames/nowalkfn/noContentSuffixes)
- S1.3: github.com/simon987/sist2 (README.md, USAGE.md)
- S1.4: Apple Support docs (Spotlight privacy), Eclectic Light Company blog
  (`.metadata_never_index` Sequoia breakage), `tmutil`/`CSBackupSetItemExcluded`
  Apple developer docs
- S1.5: docs.syncthing.net/users/ignoring.html
- S1.6: raw.githubusercontent.com/github/gitignore/main/Global/Windows.gitignore,
  .../Global/macOS.gitignore, .../Global/Linux.gitignore,
  .../Node.gitignore (github/gitignore repo, CC0-1.0)
- S1.7: restic docs (exclude-file, --exclude-if-present, --exclude-caches),
  Backrest GitHub repo (garethgeorge/backrest), bford.info CACHEDIR.TAG spec
- S1.8: github.com/qarmin/czkawka (CLI docs, README)
- S2: PyPI `pathspec` project page + GitHub (cpburnz/python-pathspec),
  PyPI `wcmatch` project page + GitHub (facelessuser/wcmatch),
  docs.python.org/3.13 (`pathlib.PurePath.full_match`, `glob.translate`,
  `fnmatch.translate` lru_cache)
- S3.1: wiki.servarr.com/sonarr/settings, wiki.servarr.com/sonarr/system,
  github.com/Sonarr/Sonarr/issues/1592, #6658, #5926,
  github.com/Radarr/Radarr/issues/5543
- S3.2: docs.paperless-ngx.com/configuration,
  github.com/paperless-ngx/paperless-ngx discussions #6430, #8129, #5278
- S3.3: fileflows.com/docs (Library/Schedule concept pages -- proprietary,
  cited for pattern only)
- S3.4: docs.immich.app/features/libraries
- S3.5: github.com/martinkirch/drop2beets, discourse.beets.io,
  github.com/hekmon/rclonemount, github.com/nilreml/rclone-mount-systemd
- S4.1: osquery.readthedocs.io/en/stable/deployment/configuration/,
  github.com/osquery/osquery/tree/master/packs (incident-response.conf)
- S4.2: fleetdm.com/securing/what-are-fleet-policies,
  fleetdm.com/docs/configuration/yaml-files,
  fleetdm.com/docs/configuration/fleet-server-configuration
- S4.3: osquery.readthedocs.io/en/stable/deployment/file-integrity-monitoring/

*(Gap disclosed: Fleet's exact `policies` table SQL/migration file could not be
retrieved in this research session; the field list in S4.2 is reconstructed
from public docs/API shape, not a verified schema dump -- flag for a follow-up
direct-repo check before implementing Stage C.)*
