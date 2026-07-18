# W6-R2 — filearr-agent as a native Unraid plugin (Sonnet, 2026-07-18)

Research only — no production code changes. Question: what would it take to
ship `filearr-agent` (agent/, Go, CGO_ENABLED=0, self-updating via the P5-T7
Ed25519 signed-manifest pipeline) as a native Unraid **plugin** (`.plg` +
Slackware `.txz`), instead of — or alongside — a Docker container using
`-v /mnt/user:/mnt/user:ro` the way `unraid/` already ships the central
stack's four CA templates.

## Recommendation (not yet architect-ratified)

**Ship the Docker-agent-with-bind-mount first; do not build the native
plugin now.** The one architectural capability a plugin uniquely buys —
running while the Unraid **array is stopped** — doesn't help filearr-agent:
its entire job is scanning array files, so it needs the array up regardless,
which removes the exact reason the precedent plugin (Tailscale, official
LimeTech-maintained, Go binary) had to become a plugin in the first place
(§6). Filearr already ships a `library.native_prefix` remote-path-mapping
invariant (CLAUDE.md #3) built for exactly this class of problem — reuse it
to resolve a container bind-mount path back to the true `/mnt/user/...`
native path; that is materially cheaper than the CA-submission + multi-
Unraid-version-maintenance tax a plugin carries (§3, §5). Revisit the native
plugin as a **v2 option** if users specifically want array-independent
catalog/offline-search availability, or if CA moderators push back on a
Docker submission as "should be a plugin" for a filesystem-visibility tool.
If/when built, the update-path ruling in §4 is the load-bearing decision —
get that reviewed before writing any `.plg` code, since it interacts with
work already shipped (P5-T7 self-updater).

**Sharpest hazard found (§4, §5.1):** naively porting the Tailscale pattern
would break the P5-T7 self-updater. Unraid's plugin manager **re-runs the
`.plg`'s install `<FILE>` actions on every boot** (root is a RAM disk;
`/usr/local/emhttp/plugins/*` is rebuilt from scratch each boot from
packages cached on the flash boot device). If the agent's Ed25519-verified
self-update (agent/README.md — rename+re-exec swap) ever wrote its new
binary into that RAM-disk plugin directory, the *next reboot* would silently
overwrite it with the older bootstrap binary pinned in the `.plg`, discarding
the update with no error. The binary + its SQLite index/config must live in
a genuinely persistent, non-flash location (`/mnt/user/appdata/filearr-
agent/` — the same appdata convention `unraid/README.md` already uses for
Postgres/Meilisearch data), and the rc.d start script must prefer whatever's
already there over re-extracting the `.plg`'s bootstrap copy.

---

## 1. Plugin anatomy

### `.plg` XML format
A `.plg` is a single XML file the Unraid plugin manager parses top-to-bottom,
executing each `<FILE>` element in document order on install/update, and (a
subset, tagged `Method="remove"`) on removal. Verified against the current
(2026-07-16) official Tailscale plugin
[`plugin/tailscale.plg`](https://github.com/unraid/unraid-tailscale/blob/trunk/plugin/tailscale.plg)
and the community
[plugin-docs.mstrhakr.com](https://plugin-docs.mstrhakr.com/) reference
(unofficial but corroborated against that live example):

```xml
<PLUGIN name="filearr-agent" author="..." version="2026.07.18.0000"
        launch="Settings/FilearrAgent"
        pluginURL="https://raw.githubusercontent.com/.../filearr-agent.plg"
        support="https://forums.unraid.net/topic/NNNN-..." min="7.0.0">
<CHANGES><![CDATA[ ### 2026.07.18.0000 ... ]]></CHANGES>
<FILE Name="/boot/config/plugins/filearr-agent/filearr-agent-bootstrap-<ver>-x86_64-1.txz">
  <URL>https://.../artifacts/filearr-agent-linux-amd64.txz</URL>
  <SHA256>...</SHA256>
</FILE>
<FILE Run="/bin/bash"><INLINE><![CDATA[ ...install script... ]]></INLINE></FILE>
<FILE Run="/bin/bash" Method="remove"><INLINE><![CDATA[ ...remove script... ]]></INLINE></FILE>
</PLUGIN>
```

- **Version** is a free-form string compared with `version_compare()`
  semantics; the convention (Tailscale, most CA plugins) is a sortable
  `YYYY.MM.DD.HHMM` timestamp, not semver.
- **`min`/`max`** on `<PLUGIN>` gate the whole plugin to an Unraid OS range;
  per-`<FILE>` `Min`/`Max` gate individual files (Tailscale uses this pattern
  to skip newer files on older hosts and instead reinstalls a version-pinned
  historical `.plg` via an inline PHP version check — see the real script in
  their file, reproduced faithfully above in spirit).
- **Checksums**: `<SHA256>` (preferred) or `<MD5>` inside a `<FILE>` block;
  verified both pre-download (skip if already present+matching) and
  post-download (abort install with a visible error if mismatched).
- **`Run=`** determines the interpreter for inline/downloaded scripts
  (`/bin/bash`, `/usr/bin/php` — Tailscale uses a PHP pre-install block to
  detect the running Unraid version and redirect old hosts to a pinned
  historical plugin URL); omit `Run=` for a plain package `<FILE>` download.
  `Run="upgradepkg --install-new"` installs a Slackware `.txz` in place.
- **`Method="remove"`** marks a `<FILE>` block as the uninstall script only.

### Package format and boot persistence
Unraid's root filesystem is **RAM** (a Slackware live-CD-style boot, not a
persistent disk install). Only the boot flash device (`/boot`, FAT32) and
array/cache disks are physically persistent. Plugin `.txz` packages follow
Slackware's `name-version-arch-build.txz` convention and are unpacked with
`upgradepkg --install-new` / `installpkg`, which run the package's
`install/doinst.sh` post-install script. The plugin manager caches the
downloaded `.txz` under `/boot/config/plugins/<name>/` (survives reboot) and
**re-runs the `.plg`'s install directives on every boot**, re-extracting
into RAM locations like `/usr/local/emhttp/plugins/<name>/` from the cached
flash copy — this is *not* a persistent unpack, it happens fresh every boot,
confirmed both by the community filesystem-layout doc and directly observed
in Tailscale's install script (`tar xzf .../tailscale_*.tgz -C
/usr/local/emhttp/plugins/tailscale/bin` runs unconditionally on every
install/reinstall pass, i.e. every boot).

Implication for filearr-agent: the **bootstrap** binary+config glue can live
in the RAM-rebuilt tree exactly like Tailscale's, but the agent's **own**
long-lived binary (post self-update) and its SQLite index **must not** — see
the hazard note in the Recommendation and §4.

## 2. Service lifecycle (no systemd)

Unraid uses classic SysV-style `/etc/rc.d/rc.<name>` scripts
(start/stop/restart/status functions) **plus** a plugin event-hook system —
confirmed as still-current practice by the official Tailscale plugin's 2026
source tree, which ships *both*:

- `/etc/rc.d/rc.tailscale` (the actual daemon start/stop/restart control
  script — not present verbatim in the plugin's git tree because it's
  generated/shipped inside the separate `unraid-tailscale-utils` `.txz`, but
  invoked by name throughout the repo).
- **Event hooks** at `usr/local/emhttp/plugins/tailscale/event/{array_started,stopped}`
  — each a symlink to a small `restart.sh` that schedules
  `/etc/rc.d/rc.tailscale restart` via `at now` (a deliberate few-seconds
  deferral, not an inline call — avoids racing the event dispatcher).
  `doinst.sh` wires the symlinks: `ln -sf ../restart.sh array_started`.
- A **watcher**: `tailscale-watcher.php`, invoked out-of-band (cron/loop, not
  shown in the trimmed public tree) to detect a dead/unresponsive `tailscaled`
  and restart it — this exact capability shipped as a named feature in the
  2026.07.16 changelog: *"Restart tailscaled if it terminates or becomes
  unresponsive."* This is the daemon-supervision idiom to copy: Unraid has
  **no process supervisor** (no systemd, no runit) — a plugin that wants
  "restart on crash" must build and run its own watchdog loop or cron tick.

Full documented event sequence (community doc, self-consistent with the
Tailscale hook names observed above):

```
boot:  driver_loaded → starting → array_started → disks_mounted →
       svcs_restarted → docker_started → libvirt_started → started
stop:  stopping → stopping_libvirt → stopping_docker → stopping_svcs →
       unmounting_disks → stopping_array → stopped
```

**`disks_mounted` fires before `started`**, and the community doc states
array disks + user shares are guaranteed mounted by `started` — this is
*not independently confirmed against LimeTech source* in this pass (the
community doc is unofficial) but is consistent with Tailscale hooking
`array_started` (fires earlier, before user is guaranteed done) rather than
waiting all the way to `started`, suggesting real plugins hook the earliest
event that satisfies their actual dependency rather than trusting a single
"safe" event. **Action item if this plugin is built: verify empirically on
a real Unraid host** — don't take either source as ground truth; the T-item
list below includes this as an explicit verification task, not an assumed
fact, per the T3a-spike precedent of not trusting a single source for a
gating behavioral claim.

Logging convention: plain files under `/var/log/<name>/` or piping to
syslog (`logger`); Tailscale ships `/etc/logrotate.d/tailscale` (installed
by `doinst.sh`, `chmod 0644`/`chown root:root`) — logrotate is present and
usable on stock Unraid, so a plugin daemon's own log rotation should use it
rather than hand-rolling truncation.

## 3. WebGUI integration + Community Applications submission

### Minimal settings page
Unraid's Dynamix framework renders `.page` files: a short YAML-ish header
(`Menu=`, `Icon=`, `Title=`, `Type="xmenu"`, `Tag=`) followed by a `---`
separator and inline PHP/HTML. Tailscale's actual settings page is a thin
shell that delegates to an `include/page.php` renderer:

```
Menu="Tailscale"
Icon="tailscale.png"
Title="Settings"
Type="xmenu"
Tag="gears"
Markdown="false"
---
<?php
require_once "{$docroot}/plugins/tailscale/include/page.php";
echo Tailscale\getPage("Settings", true, [...]);
```

For filearr-agent, a single `.page` covering: central URL + enrollment
token entry (a form posting to a small PHP handler that shells `filearr-
agent enroll ...` or writes config read by the daemon), start/stop buttons
(calling `/etc/rc.d/rc.filearr-agent {start,stop}`), status display (parse
the daemon's own status output — the agent already exposes a local
loopback web UI per P7-T5, which a Dynamix page could even iframe/proxy),
and a log tail (`tail -n 200` of the rotated log file) is the realistic
minimum — matches Tailscale's own page count (Settings/Status/Lock/Info,
4 small pages) for a comparable "network daemon + local web surface" shape.

### CA submission requirements (ca.unraid.net/submit)
- One XML file per app; plugin entries need a `<PluginURL>` (not
  `<Repository>`, which is the Docker-template field).
- Support-thread link + project/repo link, readable name + overview,
  category.
- **Plugin submissions are always manually reviewed** (stricter than
  Docker template auto-scan).
- Submitting author needs a GitHub account with **2FA enabled**.
- **Open-source is mandatory for plugins, no exceptions**: *"Closed source
  plugins are not accepted into CA"* (contrast: closed-source Docker
  **applications** get a case-by-case pass if "reputable" — Crashplan/Plex
  cited — but the CA **template** itself must still be open; plugins get no
  such leniency because they run as root with full host access). Filearr is
  AGPL-3.0-or-later — **no issue here**.
- Explicit rejection category: *"Plugins better suited as containers."*
  This is the single biggest submission risk for filearr-agent and is
  exactly the tension this research was asked to resolve (§6). Tailscale's
  accepted precedent for clearing this bar is the array-stopped-operation
  argument — filearr-agent doesn't have an equivalent argument (§6).
- Privacy: CA's own moderation stance is "no user tracking, no data
  sharing with third parties" for the **CA index itself** (install-count
  telemetry only); this doesn't constrain what a submitted plugin's *own*
  network behavior may be — but a plugin whose only outbound calls go to
  the **operator's own self-hosted central server** (never a vendor SaaS)
  is a strictly easier case than the accepted Tailscale precedent (which
  *does* phone a third-party vendor control plane, tailscale.com, and
  clears review anyway).
  Source: [CA Application Policies & Privacy Policy](https://forums.unraid.net/topic/87144-ca-application-policies-privacy-policy/).

## 4. Update path: `.plg` version vs the agent's own Ed25519 self-updater

**Tailscale's actual pattern (does NOT self-update independently):** the
`.plg` fully pins the Tailscale binary version in its own `<FILE>` URL/SHA256
(`tailscale_1.98.9_amd64.tgz` from `pkgs.tailscale.com`) and a plugin-version
bump is *how* the binary gets updated — a human/CI bumps the `.plg`, CA users
see a plugin update, the new `.plg` re-downloads the new pinned binary. There
is no independent in-daemon self-update. This is a **materially different**
model from what P5-T7 already built for filearr-agent (Ed25519-signed
manifest, canary→promote rollout, A/B rename+re-exec swap, crash-loop
rollback with boot-counter — see `agent/README.md` "Self-update" section) —
re-plumbing filearr-agent onto the Tailscale/`.plg`-only model would throw
away that whole shipped system and lose canary rollout + crash-loop
rollback, which the `.plg` mechanism has no equivalent of (a bad `.plg`
version just breaks every subscriber immediately, no staged rollout).

**Ruling to propose (not yet verified against a live Unraid host — flag for
the T-item list):** the `.plg` delivers **only the bootstrap** — install/
remove scripts, rc.d script, event hooks, `.page` files, and a **first-run**
binary copy — into a **persistent, non-RAM** data directory
(`/mnt/user/appdata/filearr-agent/bin/filearr-agent`, matching the existing
appdata convention `unraid/README.md` already documents for the rest of the
stack). The rc.d start script always execs the appdata copy. P5-T7's
existing self-updater then owns every subsequent version change, verifying
its Ed25519 manifest against central exactly as it already does on Linux/
Windows/macOS today — **the plugin adds a new OS target to an existing
mechanism, it does not add a second update mechanism.** The `.plg`'s own
version number then tracks only the bootstrap/glue-script generation
("plugin infrastructure v3") independent of the agent binary's semver,
similar to how a Docker image tag and an in-app "check for updates" can be
decoupled.

**The reboot-clobber hazard (restated from the Recommendation, this is the
load-bearing risk of the whole ruling):** because the plugin manager re-runs
`.plg` install directives every boot, the install script must be
**idempotent and non-destructive toward a newer already-self-updated
binary** — e.g. compare the appdata binary's embedded version/manifest
digest against the bootstrap's before ever overwriting, or simply never
overwrite if the appdata binary already exists and only lay it down when
absent (first install / after a factory-reset). This exact idempotency
property is untested — **explicit verification task**, not assumed.

## 5. Constraints and hazards

### 5.1 RAM-disk root / reboot-clobber (see §4 — the sharpest constraint)
Restated once more because it's the finding most likely to bite a naive
implementation: **anything the agent's own self-updater writes must live
outside `/usr/local/emhttp/plugins/*` and outside anything the `.plg`
unconditionally re-lays-down each boot**, or a routine reboot silently
regresses the agent to whatever version is pinned in the last-installed
`.plg`.

### 5.2 Array-start timing / late-mounting roots
The agent must tolerate its scan roots (`/mnt/user/...`) not existing yet
at process start if it's ever hooked to an earlier event than `started`
(Tailscale hooks `array_started`, which per the documented sequence fires
*before* `disks_mounted`/`svcs_restarted`/`docker_started`). Filearr's agent
already has to tolerate this class of failure for any local-disk deployment
(a share unmounting mid-scan, CLAUDE.md invariant on tombstoning rather than
hard-delete) — the plugin lifecycle only adds "roots may not exist yet at
process launch," which is a strict subset of "roots may disappear at any
time," so no new resilience code should be required, only a startup retry/
backoff instead of a hard failure on an absent root.

### 5.3 Flash write endurance / FAT32 size
`/boot` is FAT32 (4 GiB **per-file** ceiling — irrelevant here, the agent
binary is tens of MB, nowhere near it) on a physical USB flash device with
finite write endurance; Unraid community guidance is explicit about
minimizing steady-state writes to it (cache/RAM-disk plugins exist
specifically to offload logs/Docker state off the flash device). Since the
ruling in §4 keeps the flash device's role to "cache the bootstrap `.txz`,
written once per plugin-version bump" (infrequent — Tailscale ships roughly
monthly), this is a non-issue under the proposed design; it becomes an issue
only if a design mistakenly routes the self-updater's frequent-poll writes
(P5-T7 default poll interval 6h) at the flash device instead of appdata.

### 5.4 Unraid version floor
Tailscale's current `.plg` pins `min="7.0.0"` (with an inline PHP fallback
to a `unraid-7.0`-tagged historical `.plg` for anyone below 7.1, showing
LimeTech actively drops support for old point releases in-band). Many
smaller community plugins still target `6.12+`. **Assumption, not verified**:
filearr-agent should target whatever floor the rest of the Filearr stack
already assumes for Docker/CA compatibility (the existing `unraid/` CA
templates don't appear to state a floor) — recommend `min="6.12.0"` unless
a specific reason (an event/API surface only available in 7.x) forces
higher; needs a real check against `docs/execution-plan.md` deployment
targets before being pinned in an actual `.plg`.

## 6. Plugin vs. Docker-with-bind-mount — the honest weigh

**The case for a plugin:** a container only sees whatever's bind-mounted at
`docker run` time; a host-level plugin process sees the live `/mnt/user`
FUSE mount and Unraid's own share/disk topology directly, with no
indirection to keep in sync. This is a real advantage in isolation.

**Why it matters less than it looks for filearr-agent specifically:**
Filearr already built `library.native_prefix` (CLAUDE.md #3) as a first-
class remote-path-mapping concept, explicitly for the case of "the absolute
path in front of the scanner isn't the source system's canonical path" —
*arr-style remote path mappings are cited as the direct analogy in the
architecture invariant itself. A Docker bind mount's translation
(`/mnt/user/... ` host path → `/mnt/user/...` container path, 1:1 if the
bind mount is `/mnt/user:/mnt/user:ro`, or needing one `native_prefix` row
if it isn't) is a strictly smaller, already-half-solved version of a problem
the codebase has already designed for. It is not a novel gap a plugin
uniquely closes.

**Why Tailscale had to be a plugin, and why that reason doesn't transfer:**
Tailscale's real, load-bearing justification (visible in its own repo
description: *"allows connection via Tailscale even if the array is
stopped"*) is that its value proposition — remote reachability into the box
— must survive the array/Docker being down, which is architecturally
impossible for a Docker container (Docker itself requires the array running
on Unraid). filearr-agent's value proposition is the inverse: it exists
*to scan the array*. If the array is stopped, there is nothing for it to do
regardless of which packaging format it ships as. The one scenario where a
plugin's array-independence would matter — serving the agent's local
offline-search web UI (P7-T5) or CLI query surface (P7-T2/T3) against a
*previously built* SQLite index while the array is stopped — is a genuine,
narrower use case, but a materially smaller slice of the value than
Tailscale's "reach the box at all" case, and is speculative until a user
actually asks for it.

**Cost side of the ledger:** a plugin adds an entirely new distribution/
maintenance surface not shared with anything else in the repo — `.plg`
authoring, Slackware packaging, rc.d + event-hook scripts, `.page` files,
a CA submission that's manually reviewed and must overcome the "better
suited as a container" rejection category head-on, and ongoing
compatibility testing across Unraid OS point releases (evidenced by
Tailscale's own 2026 changelog entries fixing plugin-side breakage across
Unraid 7.0→7.1). A Docker template adds a fifth CA XML file next to the
four `unraid/` already ships, reusing the exact same P5-T7 release pipeline,
zero new packaging format, and zero new host-version compatibility matrix.

**Verdict:** ship Docker-with-bind-mount now (§ Recommendation); keep this
document as the design for a native plugin as a documented, scoped-but-
deferred v2 option, re-triggered by either (a) explicit user demand for
array-stopped local search/query availability, or (b) CA moderator pushback
specifically citing the "better suited as a container" rejection reason on
an actual submission attempt.

## 7. Effort estimate + task breakdown (if/when this is greenlit)

| Task | Description | Size |
|---|---|---|
| PLG-T1 | Verify §2/§5.2 event-timing + reboot-reinstall claims against a real Unraid 7.x host (not just community docs) — array_started vs started, RAM-disk reinstall-on-boot behavior, idempotency of re-running install with an existing appdata binary | S (spike, ~1 host session) |
| PLG-T2 | `.plg` scaffold: PLUGIN/CHANGES/FILE skeleton, version-gate `min=`, checksum-verified bootstrap `<FILE>` pointing at an existing P5-T7 release artifact URL | S |
| PLG-T3 | rc.d script (`/etc/rc.d/rc.filearr-agent` start/stop/restart/status) execing the **appdata** binary path per §4's ruling, never the RAM-disk bootstrap copy after first install | S |
| PLG-T4 | Event hooks (`array_started` or `started` per PLG-T1's finding, `stopping`) wired via `doinst.sh` symlinks, Tailscale-style deferred restart via `at` | S |
| PLG-T5 | Install/remove script idempotency: first-install lays down the appdata binary+dirs; every subsequent boot's reinstall must detect and preserve a newer self-updated binary (the §4 hazard fix) | M — this is the task most likely to reveal surprises |
| PLG-T6 | Minimal `.page` settings UI: central URL + enrollment token form, start/stop, status, log tail (reuse P7-T5's local web UI via iframe/proxy if feasible rather than re-implementing status rendering) | M |
| PLG-T7 | Logging: syslog or `/var/log/filearr-agent/` + logrotate.d entry | S |
| PLG-T8 | Package build: `.txz` via CI (mirrors the existing `filearr-release` cross-compile step in P5-T7, add a Slackware-package wrapping stage) | S |
| PLG-T9 | CA submission: repo structure, icon, support thread, overview copy addressing the "better suited as container" concern head-on (cite §6's array-stopped-vs-not distinction, or whatever real differentiator exists by then); GitHub 2FA on the submitting account | M (review latency outside our control) |
| PLG-T10 | Ongoing: Unraid point-release compatibility testing (rc.d/event mechanics have changed under LimeTech before — Tailscale's 2026 changelog is direct evidence) | Ongoing, not one-time |

**Total one-time build, PLG-T1–T9: roughly M+ (a few focused days once
greenlit), dominated by PLG-T5's idempotency work and PLG-T6's settings
page** — small in absolute LOC (this is all shell/PHP/XML glue around an
already-built, already-self-updating Go binary) but disproportionately
fiddly to get right on a platform with no real local test harness (needs a
real or virtualized Unraid host per PLG-T1). **Recommendation stands: not
worth doing now** — the Docker-agent-with-bind-mount path ships the same
underlying binary today with none of PLG-T1–T10's surface.

## Sources

- [unraid/unraid-tailscale](https://github.com/unraid/unraid-tailscale) —
  official LimeTech-maintained Go-binary plugin, fetched 2026-07-18
  (`plugin/tailscale.plg`, `src/install/doinst.sh`,
  `src/usr/local/emhttp/plugins/tailscale/{restart.sh,event/,*.page}`).
- [Community Applications: Repository XML format](https://ca.unraid.net/submit/help/repository-xml)
  and [CA Application Policies & Privacy Policy](https://forums.unraid.net/topic/87144-ca-application-policies-privacy-policy/).
- [plugin-docs.mstrhakr.com](https://plugin-docs.mstrhakr.com/) /
  [github.com/mstrhakr/plugin-docs](https://github.com/mstrhakr/plugin-docs)
  — community-maintained (unofficial) reference for `.plg` format, event
  system, and file-system layout; used for corroboration, not as sole
  source, per §2's explicit "verify against a real host" caveat.
- In-repo: `unraid/README.md` (existing CA Docker templates + appdata
  convention), `agent/README.md` (P5-T7 self-update mechanism this design
  must not duplicate or break), CLAUDE.md architecture invariant #3
  (`native_prefix`, the existing remote-path-mapping precedent cited in §6).
