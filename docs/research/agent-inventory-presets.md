# Research Brief — Agent Inventory Presets (W6-R1)

Scope: define the predefined folder selections the distributed Go agent will
offer for inventorying user data across Windows, Linux and macOS, feeding
W6-D2 (policy push: admins assign named path selections per config group) and
W6-D3 (inventory collectors: per-file stat/owner/ACL metadata). Researched
2026-07-18 against current OS vendor documentation, `agent/internal/scan/`
(the Go walker: `presets.go`, `walker.go`, `gitignore.go`) and
`backend/filearr/presets.py` (the central exclusion-preset bundles this work
must reuse, not fork). No production code was changed for this brief.

---

## 0. tl;dr

Six named presets are proposed: `user-documents`, `user-media`,
`user-profiles-full`, `downloads`, `server-data`, `custom` (empty scaffold).
Each preset is a set of per-OS path expansions plus a reference to the
**existing** `backend/filearr/presets.py` bundle names (`system_files`,
`hidden_dotfiles`, `caches_temp`, `node_modules_build`, `os_metadata`) —
no new exclusion-pattern vocabulary is introduced, only new *root path*
vocabulary layered on top of the pattern engine that already ships.

The three sharpest gotchas, in order of how badly they'd corrupt an
inventory if missed:

1. **OneDrive Known Folder Move (KFM) silently redirects Documents/
   Pictures/Desktop into the OneDrive tree, and the redirected files are
   frequently cloud-only placeholders.** A naive `%USERPROFILE%\Documents`
   string expansion works whether or not KFM is active (the known-folder
   *path* changes, `%USERPROFILE%` does not), but the walker MUST check
   `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` / `FILE_ATTRIBUTE_RECALL_ON_OPEN`
   before opening file contents anywhere under a user profile — otherwise a
   size-only inventory scan silently hydrates (downloads) an entire OneDrive
   library, burning bandwidth and local disk quota the user never asked to
   spend. `os.Stat`/`GetFileAttributesEx`-class calls are safe (do not
   trigger recall); `os.Open`/`ReadFile` are not.
2. **XDG user directories are locale-translated, not fixed English strings.**
   `~/.config/user-dirs.dirs` is authoritative; a preset that hardcodes
   `~/Documents` will silently miss a German (`~/Dokumente`), French
   (`~/Documents` — coincidentally same, but Spanish is `~/Documentos`,
   Portuguese `~/Documentos`, etc.) or otherwise localized home directory
   layout, and worse, on a system where `xdg-user-dirs-update` never ran
   (minimal/server distros, some WMs) the file may not exist at all and the
   fallback must be the literal `$HOME` (not a guessed English folder name).
3. **macOS Full Disk Access (FDA/TCC) cannot be granted programmatically to
   an arbitrary background daemon.** TCC enforces against root itself; a
   LaunchDaemon running as root still gets `EPERM`/silent empty listings
   under `~/Documents`, `~/Desktop`, `~/Downloads`, `~/Pictures` (iCloud
   Photos), Mail, and other TCC-protected locations unless the *specific
   signed binary* has been manually added to Full Disk Access in System
   Settings (or pushed via an MDM profile — viable for the fleet case but
   still an explicit, auditable grant, never silent). The inventory design
   must treat "protected location, zero entries returned" as an expected,
   distinguishable state (not a bug, not "empty folder") and surface it to
   the admin rather than reporting a false-negative empty inventory.

Full preset table: §5. Path-expansion syntax additions needed beyond plain
env-var text substitution: §1.1 (Windows known-folder tokens), §2.1 (XDG
directory-file parsing). Collector attribute APIs: §6.

---

## 1. Windows

### 1.1 Known-folder discovery — do not string-expand, resolve via API

Documents/Pictures/Music/Videos/Desktop/Downloads have stable
[`KNOWNFOLDERID`](https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid)
GUIDs (`FOLDERID_Documents`, `FOLDERID_Pictures`, `FOLDERID_Music`,
`FOLDERID_Videos`, `FOLDERID_Desktop`, `FOLDERID_Downloads`). The correct
discovery API is
[`SHGetKnownFolderPath`](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath)
(Vista+), **not** the legacy `CSIDL`/`SHGetFolderPath` pair and **not** a
literal `%USERPROFILE%\Documents` string. The reason this matters
concretely: OneDrive's **Known Folder Move (KFM)** feature repoints the
known-folder *registry values* (`User Shell Folders` under
`HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell
Folders`) to a path inside the user's OneDrive sync root, e.g.
`C:\Users\<user>\OneDrive\Documents`, while `%USERPROFILE%\Documents` may
still exist as a stale/empty leftover folder. A policy path field of literal
`%USERPROFILE%\Documents` therefore scans the **wrong, possibly-empty**
folder on any machine with KFM enabled — which is Microsoft's default nudge
for any consumer/M365 OneDrive-signed-in machine.

**Design implication for W6-D2's path syntax**: known folders need a
distinct token from plain `%ENV%` expansion — e.g. `{KF:Documents}` —
resolved by the agent at collection time via `SHGetKnownFolderPath`
(Go: `golang.org/x/sys/windows.KnownFolderPath(folderid, flags)`, already a
published wrapper in `x/sys/windows`), not by textual substitution against a
static env-var table. `%USERPROFILE%`-style literal paths remain useful as
a *fallback preset* (`user-profiles-full`, §5) that intentionally walks the
raw profile tree regardless of redirection.

Detecting whether a given resolved folder is itself OneDrive-redirected
(useful for UI/audit, not required for correctness): the resolved path
containing `\OneDrive` is the practical signal Microsoft's own support
content uses; there's no dedicated "is this folder redirected" boolean API
distinct from just comparing the resolved path.

### 1.2 Cloud placeholder detection (OneDrive Files On-Demand)

A file/dir that is a Files-On-Demand placeholder carries
`FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` (0x00400000) and/or
`FILE_ATTRIBUTE_RECALL_ON_OPEN` (0x00040000) in the attributes returned by
`GetFileAttributesEx`/`FindFirstFile` — both are metadata-only calls, safe
to use for a size/attribute inventory pass. **Do not** call `CreateFile` +
`ReadFile` (or Go's `os.Open`+read) on a path carrying either bit unless the
collector explicitly wants to force a download; doing so recalls (downloads)
the file transparently. A secondary, coarser signal usable from a plain
`os.Lstat`-class call: on NTFS, a cloud placeholder reports
`Size > 0` but allocated blocks `== 0` (no local data blocks). The most
precise signal is the reparse-point tag itself
(`IO_REPARSE_TAG_CLOUD` family, `0x9000001A`–`0x9000F01A`, and
`IO_REPARSE_TAG_FILE_PLACEHOLDER`, `0x80000015`) read via
`FindFirstFile`'s `dwReserved0` when `FILE_ATTRIBUTE_REPARSE_POINT` is set —
Go's `x/sys/windows` does not name these OneDrive-specific tag constants
today, so the collector will need to define the numeric constants itself
(same pattern already used for `Icon[\r]`-class "transcribe the exact
spec value" work in this codebase).

Practical collector rule: **inventory (list + stat) placeholders normally;
never read their content; surface "cloud-only, not hydrated" as a distinct
item state** rather than treating a 0-byte-on-disk placeholder as an empty
file.

### 1.3 Multi-user enumeration

Two candidate approaches:

- **Filesystem**: enumerate `C:\Users\*`.
- **Registry**: enumerate
  `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList`, one
  subkey per profile SID, reading `ProfileImagePath` for the path and
  filtering `Sid` against the well-known service SIDs (`S-1-5-18` SYSTEM,
  `S-1-5-19` LOCAL SERVICE, `S-1-5-20` NETWORK SERVICE) plus checking
  `FullProfile == 1` where present.

**Recommendation: registry-driven, filesystem as a display/sanity
cross-check.** The registry is authoritative for the actual set of loaded
user profiles (it also catches non-default `ProfilesDirectory` layouts,
e.g. redirected profile stores on domain-joined machines) where a bare
`C:\Users\*` glob cannot distinguish a real user profile from `Default`,
`Default User` (legacy alias), `Public`, `All Users` (legacy junction), or a
leftover/orphaned directory from a deleted account. Exclusion list for
`C:\Users\*`-glob mode specifically: `Default`, `Default User`, `Public`,
`All Users`, `DefaultAppPool`, and any directory whose name matches a
service-account convention (`*$`, `WDAGUtilityAccount`) — but this is
strictly a fallback for machines the agent can't read the registry on;
registry enumeration is preferred because it needs no such guess-list.

### 1.4 AppData: Roaming vs Local

`%APPDATA%` (Roaming, `FOLDERID_RoamingAppData`) and `%LOCALAPPDATA%`
(`FOLDERID_LocalAppData`) are both **excluded by default** from every
user-data preset — they are application config/cache/state, not user
content, and Roaming in particular round-trips through domain profile sync
on enterprise networks (inventorying it is both noisy and a potential
privacy/perf problem on a domain-joined fleet). `user-profiles-full` (§5)
is the one preset that walks the whole profile and therefore does traverse
AppData; it exists specifically for the "capture everything, let admin
filter" use case, with AppData exclusion available as an opt-in toggle on
top via the existing `caches_temp`/`node_modules_build`-style pattern
bundles (most AppData cache noise — browser caches, node_modules under
`AppData\Roaming\npm`, etc. — is already covered by patterns that exist).

### 1.5 System directories — always exclude, no exceptions

`C:\Windows\`, `C:\Program Files\`, `C:\Program Files (x86)\`,
`C:\ProgramData\` (mixed: some real app data lives here, but it's
system/service-owned, not "user data" for this feature — excluded),
`$Recycle.Bin\` (already a `system_files` pattern), `System Volume
Information\` (already a pattern), `pagefile.sys`, `hiberfil.sys`,
`swapfile.sys` (these are usually inaccessible/locked anyway, but exclude
explicitly to avoid a stat-permission-error noise item per scan),
`Thumbs.db`/`desktop.ini` (already `system_files`/`os_metadata` patterns).
None of this needs new pattern work — `system_files` + `os_metadata`
already cover it; the only new work here is making sure `user-profiles-full`
and `custom` presets apply those bundles by default rather than shipping
unfiltered.

---

## 2. Linux

### 2.1 XDG user directories — parse the file, never hardcode English names

Per the freedesktop.org `xdg-user-dirs` spec, the source of truth is
`$XDG_CONFIG_HOME/user-dirs.dirs` (defaults to `~/.config/user-dirs.dirs`),
a shell-syntax file defining `XDG_DESKTOP_DIR`, `XDG_DOWNLOAD_DIR`,
`XDG_TEMPLATES_DIR`, `XDG_PUBLICSHARE_DIR`, `XDG_DOCUMENTS_DIR`,
`XDG_MUSIC_DIR`, `XDG_PICTURES_DIR`, `XDG_VIDEOS_DIR`. `xdg-user-dirs-update`
runs at session start and **localizes the folder names to the user's
locale** the first time it runs (e.g. `$HOME/Documents` vs
`$HOME/Dokumente` vs `$HOME/Documentos`) — this is the single biggest
source of a wrong hardcoded-path preset. The collector must:

1. Read and shell-parse `user-dirs.dirs` per user (it's `KEY="$HOME/value"`
   lines — trivial to parse without a shell, just strip `$HOME/` prefix and
   quotes).
2. If the file is missing (minimal installs, some window managers/desktops
   never invoke `xdg-user-dirs-update`, headless/server systems almost
   always lack it), fall back to `$HOME` itself as the root for
   `user-documents`/`user-media`, not a guessed English folder name.
3. `user-dirs.dirs` also supports a companion `user-dirs.locale` file and a
   system-wide default template at `/etc/xdg/user-dirs.defaults` — lower
   priority, only consulted if the per-user file is absent and a distro
   default is preferred over bare `$HOME`.

### 2.2 `/home/*` enumeration

Enumerate via `/etc/passwd` (`getent passwd` semantics: UID range, typically
`>= 1000` and `< 65534`, shell not `/usr/sbin/nologin` or `/bin/false`) with
the *user's actual home directory field* from passwd, not an assumed
`/home/<username>` path — home directories are frequently customized
(`/srv/users/<name>`, LDAP/NIS-provided non-standard homes, etc.). A bare
`/home/*` glob is a reasonable **fallback preset** for `user-profiles-full`
(mirrors the Windows `C:\Users\*` fallback rationale in §1.3) but passwd
enumeration is authoritative and should be preferred when the agent can
read it (always true for a root-installed service; a non-root agent falls
back to the glob).

### 2.3 Exclusions

Reuse existing bundles: `hidden_dotfiles` (default-on already) covers most
of this by construction. Additional Linux-specific items **not** covered by
current bundles and worth a preset-level exclude regardless of dotfile
policy (so they're excluded even when an admin explicitly re-enables
dotfiles for a `custom` preset): `.cache/` (already in `caches_temp`),
`.local/share/Trash/` (freedesktop.org trash-spec directory — not currently
in any bundle, should be added to `caches_temp` or a new small
`trash_bins` bundle since it's conceptually closer to a recycle bin than a
cache), `snap/` under home (per-app versioned sandbox data, not user
content — mirrors AppData exclusion rationale), `.var/app/` (Flatpak
per-app sandboxed data/config/cache root — same rationale). `/proc`,
`/sys`, `/run`, `/dev`, `/tmp` are never under a user-data root by
definition for these presets (they're not under `/home` or `/srv`) but
`user-profiles-full`'s "everything" framing should state explicitly that it
never walks outside the enumerated profile roots — it is not a whole-
filesystem preset.

`lost+found/` is already a `system_files` pattern (fsck-created directory
at the root of any ext-family filesystem, including sometimes inside a
separate `/home` mount).

### 2.4 Server-ish data (`server-data` preset)

`/srv` (the FHS-designated location for site-specific served data) and
`/var/www` (the near-universal Apache/nginx default docroot convention,
not FHS but ubiquitous) are proposed as a **separate, opt-in preset**
distinct from user-profile presets — this data is service-owned, not a
particular human's documents, and mixing it into `user-profiles-full` would
misrepresent scan intent to an admin auditing "what does this agent
inventory." No default exclusions beyond the standard `hidden_dotfiles`/
`caches_temp`/`node_modules_build` bundles (a `/var/www` tree is exactly
the kind of place `node_modules/`, `.git/`, vendor caches, etc. actually
show up for real).

### 2.5 NFS / automount hazards

A directory walk that stats into a stale or currently-unreachable NFS
automount point can **hang indefinitely** (not error out) — `ls`/`stat`
against a dead NFS server-backed path blocks in the kernel's NFS client
past any userspace timeout the walker sets on the syscall itself, and
`autofs`'s own `x-systemd.mount-timeout` only bounds the *mount* attempt,
not a subsequent stuck read on an already-mounted-but-now-unreachable
share. This is a direct, practical hazard for `/home` on any site using
NFS-homed accounts (common in universities/enterprises) or a `server-data`
root that happens to cross an automounted `/srv` mount. **Mitigation for
the walker, not just the preset table**: any per-entry stat/readdir call
under an inventory root should run under a bounded context/goroutine
timeout with the entry marked "unreachable, skipped" on timeout, rather
than letting one dead mount stall an entire scan — this is a collector
implementation note for W6-D3, not something a path preset can solve by
itself.

---

## 3. macOS

### 3.1 Standard folders and profile enumeration

`~/Documents`, `~/Desktop`, `~/Downloads`, `~/Movies`, `~/Music`,
`~/Pictures` are fixed, non-localized *directory names* on macOS (unlike
Linux XDG dirs) — Finder may display a localized *label* for these via
`.localized` bundle metadata, but the actual POSIX path component is always
the English name. `/Users/*` enumeration should exclude `Shared` (a
genuine multi-user shared folder, arguably still worth an opt-in
`server-data`-style preset rather than treating it as junk) and `Guest`
(guest account, typically wiped on logout — low value, exclude by default).

### 3.2 Photos Library — treat as one opaque item

`~/Pictures/Photos Library.photoslibrary` is a macOS **package**
(a directory Finder presents as a single file via a bundle bit / `.photoslibrary`
extension registration) containing an internal SQLite database and a large
nested asset tree. Recommendation: **treat it as a single inventory item**
(stat the package root, do not recurse into it) — this mirrors how Finder
and Spotlight already present it to the user, avoids reporting thousands of
internal derivative/thumbnail files as if they were independent user
photos, and avoids the real risk of a naive walker/hasher touching
internal database files Apple's own docs warn against modifying. The
walker needs a "package" detection rule (directory ending in a known
package extension, or carrying the `kMDItemContentType` package UTI) —
Filearr doesn't have this concept today; it's new surface for W6-D3, not
something `presets.py` currently expresses (worth flagging to the
architect as a possible generalizable "treat as opaque bundle" preset
knob, since `.app`, `.bundle`, `.xcodeproj`, and other macOS packages share
the same shape).

### 3.3 iCloud Drive placeholder ("dataless file") detection

macOS iCloud Drive optimizes local storage by evicting file content while
keeping a "dataless" placeholder with full metadata. Detection: `stat`/
`lstat` (or `getattrlist`) and check `SF_DATALESS` in `st_flags` — this is
a safe, non-hydrating metadata-only check, analogous to Windows'
`FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`. Practical caveat found in current
field reporting (2023 Sonoma-era changes onward): Apple's own
`NSURLUbiquitousItemIsDownloadingKey`/`NSURLUbiquitousItemDownloadRequestedKey`
Foundation APIs have become unreliable for this purpose in recent macOS
versions per developer community reports — `stat`-level `SF_DATALESS`
checking and the `brctl` CLI tool (`brctl download`/`brctl evict`,
Apple's own bidirectional-sync control tool) remain the dependable
low-level path. Go implication: `golang.org/x/sys/unix`'s `Stat_t` on
darwin does not currently expose `Flags` in a documented, stable way
sufficient for this — the collector will likely need a small cgo shim or a
raw `syscall.Syscall` to `stat(2)`/`getattrlist(2)` reading `st_flags`
directly; this should be scoped as its own small spike (mirrors the
existing `phase-5-t3a-gitignore-spike.md` precedent of bake-off/spike docs
in this repo) rather than assumed solvable with a pure-Go stdlib call.

### 3.4 TCC / Full Disk Access — document honestly (see tl;dr #3)

Expanded detail beyond the tl;dr: TCC (Transparency, Consent, and Control)
mediates access to `~/Documents`, `~/Desktop`, `~/Downloads`, the Photos
library, Mail, Messages, Time Machine backups, and other categories
*regardless of Unix permissions or root privilege* — a LaunchDaemon running
as `root` is still denied by TCC unless the specific signed executable has
been explicitly granted Full Disk Access. Concretely:

- FDA is granted per signed binary via System Settings → Privacy & Security
  → Full Disk Access, or via an MDM configuration profile
  (`com.apple.TCC.configuration-profile-policy` payload) pushed to
  enrolled fleet machines — the latter is the only viable path for a
  headless/fleet-managed agent with no interactive user to click through
  the prompt.
- Without FDA, protected-folder reads return `EPERM`/empty results
  **silently** (no distinct macOS error code that unambiguously means "TCC
  blocked this," as opposed to "genuinely empty" or "genuinely no
  permission bits") — the agent's own inventory report must therefore
  track "did I successfully read ≥1 entry under this known root" as a
  health signal per protected-category root and surface "0 items, TCC
  suspected" distinctly from "0 items, verified empty," e.g. by cross-
  checking against a `getattrlist`-only stat of the directory itself
  (which typically succeeds without FDA) vs a `readdir` (which is what TCC
  actually blocks).
- SIP (System Integrity Protection) is a separate, stricter mechanism
  protecting system files themselves (not user data) and is not relevant
  to user-folder inventory — do not conflate the two in documentation to
  admins.

---

## 4. Cross-OS junk / exclusion table

All rows below either already exist as a `PRESET_BUNDLES` entry in
`backend/filearr/presets.py` / `agent/internal/scan/presets.go` (reuse,
column "Existing bundle"), or are flagged as a genuinely new gap this
research surfaced (column "New?").

| Junk category | Examples | Existing bundle | New? |
|---|---|---|---|
| Windows OS junk | `Thumbs.db`, `desktop.ini`, `*.lnk`, `$RECYCLE.BIN/` | `system_files` | — |
| macOS OS junk | `.DS_Store`, `._*` AppleDouble, `Icon\r`, `.Trashes/`, `.Spotlight-V100/`, `.fseventsd/` | `system_files` + `os_metadata` | — |
| Linux OS junk | `lost+found/`, `.directory`, `.fuse_hidden*`, `.nfs*` | `system_files` | — |
| Caches/temp (cross-OS) | `tmp/`, `.cache/`, `__pycache__/`, `*.tmp` | `caches_temp` | — |
| CACHEDIR.TAG-signed dirs | any dir with a valid `CACHEDIR.TAG` | signature check (`is_cachedir_tagged`, not a pattern) | — |
| Dev-noise | `node_modules/`, `dist/`, `.venv/`, `target/`, `.git/` (via `hidden_dotfiles`) | `node_modules_build` (+`hidden_dotfiles` for `.git/`) | — |
| Freedesktop trash | `~/.local/share/Trash/` | none today | **Yes** — add to `caches_temp` or new `trash_bins` bundle |
| Flatpak sandbox data | `~/.var/app/` | none today | **Yes** — same rationale as AppData exclusion |
| Snap sandbox data | `~/snap/` | none today | **Yes** — same rationale as AppData exclusion |
| Windows AppData | `%APPDATA%`, `%LOCALAPPDATA%` | none today (no Windows-user-profile preset existed before this brief) | **Yes** — new, scoped to the profile-walking presets below |
| Browser cache/profiles | Chrome/Firefox/Edge profile dirs | none today, out of scope here | Flagged, not designed — see §5 note under `user-profiles-full` |
| VM/container images | `.vdi`, `.vmdk`, `.qcow2`, Docker/Podman image stores | none today | Flagged as **separate opt-in preset candidate**, not built here — large, rarely "user documents," but sometimes exactly what an admin wants inventoried (disk usage audits). Out of scope for W6-R1's minimum set; recommend a future `virtualization-images` preset. |
| macOS/Windows package bundles | `.photoslibrary`, `.app`, future Windows equivalents | none today | **Yes** — new "opaque package" walker concept, see §3.2 |

---

## 5. Preset definitions

Path syntax used below (per the W6-D2 design constraints supplied):
`%VAR%` / `$VAR` / `~` for env-var and home expansion, `*` for a single
path-segment glob (multi-user enumeration), and the two Windows/macOS/Linux
extensions this research motivates: `{KF:Documents}`-style known-folder
tokens (§1.1, Windows only) and "read `user-dirs.dirs`" as the resolution
method for the Linux XDG paths below (not a literal string — the table
lists the *fallback* literal for when the per-user file is absent, per
§2.1).

| Preset id | Windows | Linux | macOS | Include patterns | Exclude (reused bundles) | Cloud-placeholder posture | Permissions posture |
|---|---|---|---|---|---|---|---|
| `user-documents` | `{KF:Documents}` per enumerated profile | `$XDG_DOCUMENTS_DIR` from `user-dirs.dirs`, fallback `$HOME` (§2.1) | `~/Documents` per enumerated profile | none (whole tree) | `hidden_dotfiles`, `caches_temp`, `os_metadata`, `system_files` | List/stat placeholders, never open content (§1.2, §3.3) | Windows: per-user, no elevation needed beyond normal profile read. Linux: readable if agent runs as root or the target user. macOS: **requires FDA grant** (§3.4) — document this on the preset itself, not just in general docs. |
| `user-media` | `{KF:Pictures}`, `{KF:Videos}`, `{KF:Music}` per profile | `$XDG_PICTURES_DIR`, `$XDG_VIDEOS_DIR` (no XDG videos var historically standardized on some distros — verify per-distro; fallback to `$HOME/Videos` literal if absent from `user-dirs.dirs`), `$XDG_MUSIC_DIR` | `~/Pictures`, `~/Movies`, `~/Music` | Extension-group refinement optional (reuse `EXTENSION_GROUPS` from `presets.py` §, e.g. `raw_photos`, `lossless_audio`) | Same as `user-documents` | Same as above; **`Photos Library.photoslibrary` treated as one opaque item, not recursed** (§3.2) | Same FDA caveat for macOS (Photos library specifically is its own TCC category, separate from Pictures folder access) |
| `user-profiles-full` | `%USERPROFILE%\*` (literal, all profiles enumerated per §1.3) minus AppData | `$HOME` per enumerated user (passwd-driven, §2.2), whole tree | `/Users/*` per enumerated profile, whole tree | none | All five existing bundles + new Flatpak/Snap/Trash exclusions (§4) | Same placeholder posture, now over a much larger surface — this is the preset where OneDrive/iCloud hydration risk is highest, flag prominently in admin UI copy | Broadest permission need of any preset; macOS FDA is effectively mandatory for this preset to be meaningful at all |
| `downloads` | `{KF:Downloads}` per profile | `$XDG_DOWNLOAD_DIR`, fallback `$HOME/Downloads` | `~/Downloads` per profile | none | Standard bundles | Same placeholder posture (Downloads is not typically OneDrive/iCloud-redirected by default, but can be if a user manually relocates it — check attributes anyway, cheap) | Same as `user-documents` |
| `server-data` | *(not applicable — Windows has no FHS equivalent; leave unset/empty on Windows agents)* | `/srv`, `/var/www` | *(not applicable by convention; macOS servers are rare and non-standardized enough to omit from the minimum set)* | none | Standard bundles (dev-noise bundle especially relevant here, §2.4) | N/A (server data is not cloud-placeholder-backed in the same way) | Root or web-server-group read typically required; no TCC/FDA concerns (not under a `/Users` home) |
| `custom` | empty scaffold | empty scaffold | empty scaffold | admin-defined | admin-defined (any subset of the five bundles + new ones) | Admin must explicitly acknowledge placeholder posture per §1.2/§3.3 in the UI when a custom root falls under a known cloud-sync folder — the agent can heuristically warn (path contains `OneDrive`/`iCloud Drive`) but cannot guarantee coverage for arbitrary custom roots | Admin-defined; UI should surface the FDA/registry caveats as contextual help, not silently assume they're handled |

Notes on the table:

- Every OS column resolves multi-user enumeration the way each OS-specific
  section above recommends (registry-first on Windows, passwd-first on
  Linux, `/Users/*` glob on macOS — macOS has no passwd-equivalent
  authoritative source cheaper than Directory Services / `dscl`, and for a
  typical single-or-few-account Mac the glob is adequate; a `dscl . -list
  /Users UniqueID` refinement is a possible future improvement, not
  required for the minimum set).
- `user-media`'s extension-group refinement is optional precisely because
  `presets.py`'s `EXTENSION_GROUPS` mechanism (R5, union semantics) already
  solves "narrow a media type to specific extensions" — this preset table
  should not reinvent that, just reference it.
- Browser cache/profile exclusion is intentionally **not** built into any
  preset above (flagged in §4 as out of scope) — browser profile dirs
  contain both junk (cache) and real user data (bookmarks, saved
  passwords-adjacent files an admin may legitimately want inventoried or,
  more likely, may legitimately want to *guarantee excluded* for privacy).
  This needs its own product decision, not a research-brief default; punting
  it to `custom` for now is deliberate, not an oversight.

---

## 6. Permissions/attribute collection for W6-D3

What is cheaply available per OS from a single stat-class syscall (i.e.,
already paid for by the walk itself, not an extra round trip):

- **POSIX (Linux, macOS)**: `os.Lstat` → `.Sys().(*syscall.Stat_t)` exposes
  `Uid`, `Gid`, `Mode` (permission bits), `Nlink`, device/inode. This is a
  zero-extra-cost read — the walker already calls `Lstat`-equivalent for
  every entry. Field names differ slightly between Linux and Darwin
  builds of `syscall.Stat_t` (e.g. `Atim`/`Mtim`/`Ctim` on Linux vs
  `Atimespec`/`Mtimespec`/`Ctimespec` on Darwin) — this needs Go build-tag
  isolation (`//go:build linux` / `//go:build darwin`), not a runtime type
  switch, matching this codebase's existing GOOS-conditional file
  convention.
- **POSIX extended attributes / xattrs** (Linux, macOS, FreeBSD): a
  *separate* syscall per file (`getxattr(2)`/`Getxattr`/`Lgetxattr` via
  `golang.org/x/sys/unix`, or the `github.com/pkg/xattr` wrapper if a
  higher-level API is preferred) — not free, adds one syscall per file if
  xattr collection is enabled. This is where macOS's `com.apple.quarantine`,
  Finder tags, and (per §3.3) potentially `SF_DATALESS`-adjacent metadata
  live; also where Linux ACLs surface indirectly (POSIX ACLs are exposed
  via the `system.posix_acl_access`/`system.posix_acl_default` xattr
  names, decodable but requiring the ACL binary-format parser, not just a
  string read). Recommend xattr collection be an **opt-in** collector flag
  given the per-file syscall cost, not part of the default stat pass.
- **Windows ACL/owner**: not available from a basic stat-equivalent call —
  requires an explicit security-descriptor query. `golang.org/x/sys/windows`
  exposes `GetNamedSecurityInfo`/`GetSecurityInfo` returning a
  `*SECURITY_DESCRIPTOR` with `.Owner()` (owner SID) and `.DACL()` (nil DACL
  = fully permissive, a real and meaningful distinct state, not an error)
  accessor methods. This is materially more expensive than the Windows
  equivalent of a stat call (`GetFileAttributesEx`) — an explicit,
  separate Win32 call per file with its own ACL-parsing cost — so it should
  be gated the same opt-in way as POSIX xattr collection, not default-on
  for a plain inventory pass. SID-to-username resolution (`LookupAccountSid`)
  is a further optional, further expensive step (potential domain-controller
  round trip on a domain-joined machine) — recommend collecting raw SIDs by
  default and resolving to names lazily/on-demand in the UI, not during the
  scan.
- **Cost caveat common to all three platforms**: none of the
  owner/ACL/xattr APIs above are free relative to the basic
  Lstat/GetFileAttributesEx the walker already does for size/mtime/type —
  budget them as an explicit "deep inventory" mode distinct from the
  default "fast inventory" (name/size/mtime/media-type) pass, mirroring how
  the central scanner already treats extraction (`metadata`) as a deferred
  post-commit job rather than inline with the walk (CLAUDE.md invariant
  #5) — the same batching/deferral philosophy applies here.

---

## 7. Open questions for the architect

1. Should `{KF:Documents}`-style known-folder tokens and "resolve via
   `user-dirs.dirs`" be a first-class part of the W6-D2 path-expansion
   grammar (a new token/resolution kind), or should the agent instead
   resolve presets to concrete literal paths locally and report *those*
   back as the effective policy (keeping the server-side grammar to plain
   env-var/glob/regex, with all the OS-specific resolution logic living
   entirely client-side in the preset table)? This brief assumes the
   latter is simpler to reason about centrally (admin UI shows resolved
   paths per agent) but doesn't resolve the tradeoff — a genuine W6-D2
   design decision, not a research finding.
2. Trash/Flatpak/Snap exclusions (§4, "New?" rows) — confirm whether these
   land in existing bundles (`caches_temp`, extending its scope) or a new
   bundle, and whether that's a `presets.py`-first change (central,
   ported to Go per the existing sync discipline) or agent-only since
   they're OS-specific concepts the central scanner (which scans arbitrary
   library mounts, not user profiles) has never needed.
3. The "opaque package" concept for `.photoslibrary` (and by extension
   `.app`, other bundle-extension directories) is new surface with no
   existing analog in `presets.py`/`PresetBundle` — worth its own small
   design note before W6-D3 implementation rather than folding into the
   preset table silently.
4. VM/container image opt-in preset and browser-profile handling are
   explicitly out of scope for this brief's minimum set (§4) — confirm
   they're tracked somewhere (future-roadmap.md candidate) so they aren't
   lost.
