// UI-T5 — single source of truth for field help text. Every labelled setting in
// the add-library form and the edit modal references a key here so wording stays
// consistent and maintainable. Values are PLAIN TEXT and rendered as text (never
// {@html}) by Help.svelte. Preset/extension-group descriptions that vary per
// bundle come from GET /presets at runtime (see caveat / label there); the
// entries below are the general, field-level explanations.

export const HELP: Record<string, string> = {
  filter_builder:
    "The Filter builder compiles your visual conditions into the same filter DSL "
    + "used by custom reports and tests it LIVE against your real catalog (RBAC-"
    + "scoped) as you edit — showing matching items and a match count. Rows are "
    + "combined with AND (the query grammar has no OR groups yet). From here you can "
    + "copy the DSL, Save as a custom report (for full exports), or Open in search "
    + "(mapping the conditions a 1:1 search filter exists for: kind, extension, size, "
    + "modified, tag, hash — other conditions are listed and dropped). Fuzzy (~) "
    + "terms are search-only and not offered here.",

  report_formats:
    "Reports export in five formats. JSON is the paginated on-screen view. The "
    + "Download menu offers four full-result machine-readable exports: CSV "
    + "(spreadsheet-friendly, formula-injection-guarded), NDJSON (one JSON object "
    + "per line, ideal for log/stream ingestion), XML (a flat <report><row><col "
    + "name=…> document), and XLSX (a native Excel workbook, streamed with bounded "
    + "memory; every cell is written as a literal string so a value like =SUM(A1) "
    + "is never evaluated as a formula). Per-item reports also carry item_id plus "
    + "full path context (path, native_path, share_url) in JSON/NDJSON/XML; item_id "
    + "is never written to CSV or XLSX.",

  report_exports_background:
    "A large export can run as a BACKGROUND job instead of a live download: it "
    + "streams to a staging file on the server (bounded memory, disk-space "
    + "guarded) and appears in the Exports panel, where you download it once it is "
    + "complete. Artifacts expire after a retention window (default 7 days) and are "
    + "then purged; the job row is kept for audit. Downloading re-checks your "
    + "permissions at fetch time.",

  report_schedules:
    "A scheduled report runs a canned or custom report on a cron schedule "
    + "(authored with the same friendly Off/Hourly/Daily/… builder as scans; stored "
    + "in UTC) and delivers the result through a notification channel. Email "
    + "attaches the file when it is small enough, otherwise it sends a link with a "
    + "row-count summary; a webhook always receives a JSON summary plus a download "
    + "URL (never the file inline). Each occurrence fires at most once, even across "
    + "worker restarts. A delivery failure raises an operational alert.",

  webhook_format:
    "How Filearr shapes the JSON body it POSTs to a webhook channel. "
    + "GENERIC is Filearr's native payload, signed with an X-Filearr-Signature "
    + "HMAC header so your receiver can verify it — unchanged from before. "
    + "DISCORD wraps the alert in a Discord message (content + a coloured embed "
    + "with the rule/event, key fields and a timestamp) so a Discord webhook "
    + "accepts it instead of rejecting an 'empty message'. SLACK sends text + a "
    + "simple block section. Discord/Slack skip the HMAC header (those endpoints "
    + "don't verify it). The format is auto-detected from the URL when you create "
    + "a channel and stays editable; SSRF pinning, no-redirect and timeout "
    + "protections are identical for every format.",

  report_download_scope:
    "Viewing a report on screen (the JSON page) needs only search permission, but "
    + "EXPORTING one (CSV/NDJSON/XML/XLSX, a background job, or a scheduled "
    + "delivery) requires the stronger 'download' permission — bulk metadata "
    + "leaving the server is treated like a download. A scoped user's export "
    + "contains only rows they are allowed to download. Admins and API keys are "
    + "unaffected.",

  name:
    "Human-readable label for the library. Identity is (library_id, rel_path), " +
    "so renaming is cosmetic and never re-anchors items.",

  root_path:
    "Absolute path INSIDE the container to the top of this library's tree " +
    "(e.g. /data/media/movies). Item identity is (library_id, rel_path) relative " +
    "to this root — changing it re-anchors every rel_path. Use the folder picker " +
    "to browse allowlisted mounts, or type a path directly.",

  native_prefix:
    "Optional source-system path prefix used to translate container paths back to " +
    "their native form (like an *arr remote path mapping). Example: container root " +
    "/data/media maps to a NAS share //nas/media or /mnt/user/media. Leave blank if " +
    "the container path IS the native path.",

  share_prefix:
    "Optional NETWORK LOCATION of this library's root as YOU open it on your own " +
    "computer — what you'd type into a file manager. Three formats are understood: " +
    "a Windows UNC path (\\\\tower\\media), an smb:// URL (smb://tower/media), or a " +
    "local mount point (/Volumes/media). This is DISTINCT from the native prefix " +
    "(which is about source-system path translation): the share location powers the " +
    "\"Open via network\" links and copy-path buttons in the breadcrumbs and browse " +
    "view. Note: browsers commonly BLOCK opening file:// links from a web page " +
    "(Chrome/Edge do this silently), so if clicking Open does nothing, use the paired " +
    "Copy path button and paste it into your file manager. AUTO-POPULATION: when the " +
    "Proxmox deploy mounted this library's storage, it recorded the real share URL, so " +
    "this can be left blank and the network location is filled in automatically (shown " +
    "as an \"auto from mount\" hint) — and it stays correct across redeploys/remounts. " +
    "Type a value only to OVERRIDE the auto-detected one.",

  os_path_format:
    "Network paths come in two OS-native spellings: an smb:// URL "
    + "(smb://tower/media) that Linux and macOS file managers open, and a Windows "
    + "UNC path (\\\\tower\\media) that Windows Explorer opens — smb:// does NOT "
    + "work in Windows, and a bare UNC is meaningless on Linux. The Paths selector "
    + "in the header controls which spelling every Open/Copy affordance shows: Auto "
    + "follows the browser's detected OS (Windows -> UNC, otherwise smb:// URL), or "
    + "you can force one. The choice is remembered in this browser. When a location "
    + "has no UNC form (an sftp/ftp/nfs/webdav share or a local mount point), the "
    + "smb:// URL / mount path is shown regardless. API responses carry BOTH forms "
    + "(share_url + share_unc / share_prefix_effective + share_unc_effective) so a "
    + "calling system can pick the one its OS needs.",

  enabled:
    "When off, the library is skipped by scheduled scans and hidden from scan " +
    "triggers. Existing indexed items remain searchable until re-scanned or deleted.",

  media_types:
    "Restrict this library to specific media types (video, audio, image, etc.). " +
    "None selected = index every supported type found under the root.",

  include_globs:
    "Optional allowlist of gitignore-style path globs (one per line). When set, ONLY " +
    "files matching an include glob are considered. Leave empty to include everything " +
    "not excluded. Matched against the path relative to the root.",

  exclude_globs:
    "Gitignore-style path globs to skip (one per line), e.g. **/.stfolder/** or " +
    "**/*.partial. Applied after includes. Prefer preset bundles below for common " +
    "junk; use this for library-specific exclusions.",

  presets:
    "Reusable exclude bundles for common junk (dotfiles, node_modules, sample/proof " +
    "folders, etc.). Default-on bundles apply automatically unless you opt out here. " +
    "Each bundle lists its exact patterns and any caveat inline.",

  extension_groups:
    "Narrow a media type to a curated set of extensions (union of the enabled groups " +
    "for that type). No group enabled = every extension mapped to that type is indexed.",

  scan_stop:
    "Stop (keep progress) gracefully ends a running scan: it finishes the " +
    "current batch, keeps every file scanned so far (and their queued metadata " +
    "extraction), then marks the run \"stopped\". It deliberately SKIPS deletion " +
    "detection — files the partial walk never reached are NOT tombstoned as " +
    "missing — so nothing is lost to a half-finished pass. The next scheduled or " +
    "manual scan is an ordinary full scan that naturally processes whatever this " +
    "run didn't reach. Use Cancel (abort) instead to stop immediately and discard " +
    "the in-flight batch.",

  job_priority:
    "Each job queue has a default priority (higher runs sooner): scan 0, extract " +
    "-10 (below scan so a fresh scan is never stuck behind a big extract backlog), " +
    "index 0, embed -20 (lowest — slow semantic embedding never starves " +
    "extraction), maintenance 0, alerts 5 (above the default lane for timely " +
    "notifications). Use the stepper to bump a queue's PENDING (queued) jobs; jobs " +
    "already running keep the priority they started with and are never preempted. " +
    "Defaults are set at defer time and configurable via FILEARR_PRIORITY_* env; " +
    "the stepper is a one-shot adjustment of the current backlog. Range -100..100.",

  staged_pipeline:
    "With the staged pipeline on (default), a scan finishes walking the whole " +
    "library BEFORE its metadata extraction runs, so the directory walk and the " +
    "extract workers don't compete for the same disk/network at once — the scan " +
    "completes faster, then post-processing runs. Tradeoff: extracted metadata " +
    "appears only after the scan completes rather than trickling in during the " +
    "walk. Set FILEARR_STAGED_PIPELINE=false to restore trickle-in extraction.",

  scan_cron:
    "Schedule automatic scans. The builder offers Off / Hourly / Daily / Weekly / " +
    "Monthly using your LOCAL time and generates the underlying cron for you; pick " +
    "Advanced to type a raw 5-field cron (cronsim syntax) directly, e.g. 0 4 * * * = " +
    "04:00. Schedules are STORED and evaluated in UTC — the builder converts your " +
    "local time (and shifts the weekday/day-of-month if the conversion crosses " +
    "midnight). Because storage is fixed-UTC, the local run time drifts ±1h across " +
    "DST. Off = no schedule (manual scans only). A tick that lands while a scan is " +
    "already running is skipped.",

  watch_mode:
    "Watch the filesystem (inotify) for near-real-time indexing. LOCAL PATHS ONLY — " +
    "inotify is unreliable over SMB/NFS/FUSE mounts, so the server rejects watch mode " +
    "on a network root. Use scan_cron for network libraries.",

  hash_policy:
    "Content-hashing strategy. auto: local roots full-hash, network mounts use the " +
    "quick (partial) hash only. full: always compute the full content hash (up to the " +
    "size ceiling). quick_only: never full-hash. Quick hashes are cheap; full hashes " +
    "give exact dedupe/identity at the cost of reading whole files.",

  hash_ceiling:
    "Per-library override (in bytes) of the global full-hash size ceiling. Files larger " +
    "than this fall back to the quick hash even under a full policy. Blank = use the " +
    "global default (FILEARR_SCAN_HASH_FULL_MAX_BYTES).",

  ocr_enabled:
    "Run OCR (Tesseract) on this library's images and scanned (image-only) PDFs to " +
    "make the text inside them searchable. OFF by default because OCR is CPU-heavy: " +
    "each image/page is rasterised and run through Tesseract. Filearr skips files that " +
    "already have a real text layer, caches the result keyed on the file's hash (an " +
    "unchanged file is never re-OCR'd), and bounds pages/pixels/time. Leave off for " +
    "libraries of movies/music/normal documents; turn on for scanned paperwork, " +
    "receipts, screenshots, or photos of text.",

  expose_gps:
    "Allow this library's GPS / location metadata (latitude, longitude, altitude from " +
    "photo EXIF) to appear in search results and the API. OFF by default for privacy: " +
    "location is extracted and stored, but the server strips it from every response and " +
    "from the search index unless you opt in here. Publishing home/location coordinates " +
    "is a recognised data-exposure risk (CWE-1230), so Filearr hides GPS by default and " +
    "only reveals it for libraries you explicitly mark safe.",

  scan_paths:
    "Optional sub-paths under the root with their own schedule/watch overrides (hot " +
    "folders). Each rel_path is relative to the root ('' = whole library); a null " +
    "override inherits the library's schedule.",

  // P4-T3 custom fields
  cf_name:
    "The user_metadata key this field governs (e.g. shelf_location). Normalised to " +
    "lowercase [a-z0-9_] with no leading digit; names that collide with a core column " +
    "or the reserved cf_/underscore prefixes are rejected. IMMUTABLE after creation — " +
    "a rename would orphan existing values, so create a new field instead.",

  cf_data_type:
    "How values written under this key are validated on edit: string, integer, float, " +
    "boolean, date, url, or select. IMMUTABLE after creation (a retype would " +
    "misinterpret existing values).",

  cf_libraries:
    "Restrict this field to specific libraries. None selected = applies to ALL " +
    "libraries. For an item in a non-listed library the key is treated as unregistered " +
    "(ad-hoc), so its value is stored but never type-checked.",

  cf_required:
    "Display-only hint in v1 (marks the field as expected in the edit UI). It is NOT " +
    "enforced on write — omitting the value never fails a save.",

  cf_facet_sort:
    "Facetable makes the field a search filter; sortable makes it a sort key. Both take " +
    "effect only after an index rebuild (P4-T6) — toggling here records the intent.",

  job_history_retention:
    "Finished job records (succeeded / failed / cancelled / aborted) are kept " +
    "for FILEARR_JOB_HISTORY_RETENTION_DAYS (default 14) and then hard-deleted " +
    "nightly by the purge_job_history maintenance task, so the failed-jobs list " +
    "and succeeded backlog never grow without bound. Queued (todo) and running " +
    "(doing) jobs are never purged regardless of age. Because succeeded rows also " +
    "feed the queue-card \"done\" counters and the extract-rate ETA, those become " +
    "windowed to the retention period — expected, not a fault. Use \"Clear failed " +
    "history\" to wipe failed records immediately without waiting for retention.",
};


/** Lookup helper that tolerates a missing key (returns '' so callers stay simple). */
export const help = (key: string): string => HELP[key] ?? "";

// --- FIX-8: grouped view for the standalone Help page (#/help) --------------
// The inline "?" popovers reference HELP by key at each field. The Help PAGE
// renders the SAME copy grouped by topic so a user can read every setting's
// explanation in one place. Keys are listed per topic in a sensible reading
// order; a human label per key drives the page headings. Any HELP key not
// listed here still appears under "Other" (see HelpPage), so adding a HELP entry
// never silently drops it from the page.
export interface HelpTopic {
  title: string;
  /** [helpKey, humanLabel] in display order. */
  items: [string, string][];
}

export const HELP_TOPICS: HelpTopic[] = [
  {
    title: "Library basics",
    items: [
      ["name", "Name"],
      ["root_path", "Root path"],
      ["enabled", "Enabled"],
      ["native_prefix", "Native path prefix"],
      ["share_prefix", "Network (share) location"],
      ["os_path_format", "Network path format (smb:// vs UNC)"],
    ],
  },
  {
    title: "What gets indexed",
    items: [
      ["media_types", "Media types"],
      ["include_globs", "Include globs"],
      ["exclude_globs", "Exclude globs"],
      ["presets", "Exclude presets"],
      ["extension_groups", "Extension groups"],
    ],
  },
  {
    title: "Scanning & scheduling",
    items: [
      ["scan_cron", "Scan schedule"],
      ["watch_mode", "Watch mode"],
      ["scan_paths", "Hot folders (scan paths)"],
      ["scan_stop", "Stop (keep progress)"],
      ["hash_policy", "Hash policy"],
      ["hash_ceiling", "Hash size ceiling"],
    ],
  },
  {
    title: "Privacy & OCR",
    items: [
      ["ocr_enabled", "OCR"],
      ["expose_gps", "Expose GPS / location"],
    ],
  },
  {
    title: "Jobs & pipeline",
    items: [
      ["job_priority", "Job priorities"],
      ["staged_pipeline", "Staged scan-then-extract pipeline"],
      ["job_history_retention", "Job history retention"],
    ],
  },
  {
    title: "Custom fields",
    items: [
      ["cf_name", "Field name"],
      ["cf_data_type", "Data type"],
      ["cf_libraries", "Libraries"],
      ["cf_required", "Required"],
      ["cf_facet_sort", "Facetable / sortable"],
    ],
  },
  {
    title: "Reports",
    items: [["report_formats", "Report export formats"]],
  },
];
