<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import {
    cancelScan, clearFailedJobs, createLibrary, forceClearScan, stopScan, failedJobs, libraryErrors,
    listLibraries, listPresets, listScans, listShareMap, resolveShareHint,
    retryExtracts, scanEventsUrl, scanLibrary, targetedScan, getTaxonomy,
    stats as fetchStats,
    type FailedJob, type FailingItem, type Library,
    type PresetsResponse, type ScanRun, type ShareMapEntry, type TaxonomyNode,
  } from "./api";
  import TaxonomySelector from "./TaxonomySelector.svelte";
  import { HELP } from "./help";
  import Help from "./Help.svelte";
  import { formatShare, shareLocation } from "./osFormat";
  import { shareFormat, detectedPlatform } from "./osFormat.svelte";
  import FolderPicker from "./FolderPicker.svelte";
  import LibraryEditModal from "./LibraryEditModal.svelte";
  import ScheduleField from "./ScheduleField.svelte";
  import DeleteLibraryDialog from "./DeleteLibraryDialog.svelte";
  import CustomFieldsPanel from "./CustomFieldsPanel.svelte";
  import RbacPanel from "./RbacPanel.svelte";
  import UsersPanel from "./UsersPanel.svelte";
  import SessionsPanel from "./SessionsPanel.svelte";
  import AuditPanel from "./AuditPanel.svelte";
  import { gotoBrowse } from "./routes";
  import type { AuthPrincipal } from "./api";

  // P6-T11/T12: the current session principal (null when auth is disabled).
  // Gates the admin-only panels (Users, Audit) and the admin session controls.
  // `authDisabled` (FILEARR_AUTH_ENABLED=false): the API is unrestricted, so
  // panels for features that exist WITHOUT auth (Agents) must not hide behind
  // a session-derived isAdmin that can never be true. User/session/audit
  // panels stay session-gated — they are meaningless with auth off.
  let { me = null, authDisabled = false }: { me?: AuthPrincipal | null; authDisabled?: boolean } =
    $props();
  const isAdmin = $derived(!!me && me.global_role === "admin");
  // P5-T1: the distributed-agent fleet panel is opt-in (FILEARR_AGENTS_ENABLED).

  let libraries = $state<Library[]>([]);
  let scans = $state<ScanRun[]>([]);
  let error = $state("");
  let busy = $state<Record<string, boolean>>({});

  // T11 error surfacing: live per-library extraction-error counts (from /stats),
  // an expandable per-library failing-items list, and a failed-jobs table.
  let errorCounts = $state<Record<string, number>>({});
  let retrying = $state<Record<string, boolean>>({});
  let expanded = $state<Record<string, boolean>>({});
  let failing = $state<Record<string, FailingItem[]>>({});
  // FIX-8: paginated failed-jobs table (used to grow unbounded on screen).
  const FAILED_PAGE = 25;
  let failedJobsList = $state<FailedJob[]>([]);
  let failedTotal = $state(0);
  let failedOffset = $state(0);
  let clearingFailed = $state(false);

  // P2-T5 catalogue (read-only), passed to the edit modal's indexing sections.
  let presetsMeta = $state<PresetsResponse>({ presets: [], extension_groups: [] });

  // UI-T1/T2 modal targets.
  let editing = $state<Library | null>(null);
  let deleting = $state<Library | null>(null);

  // W9 targeted-scan dialog: rescan a specific file/folder path under a library
  // (automation-style — the path need not appear in the catalog yet, but must
  // exist on disk; a 404 is surfaced inline). recursive is ignored for a file.
  let targeting = $state<Library | null>(null);
  let tgtPath = $state("");
  let tgtRecursive = $state(true);
  let tgtBusy = $state(false);
  let tgtMsg = $state<{ ok: boolean; text: string } | null>(null);

  function openTargeted(lib: Library) {
    targeting = lib;
    tgtPath = "";
    tgtRecursive = true;
    tgtMsg = null;
  }

  async function submitTargeted(e: Event) {
    e.preventDefault();
    if (!targeting || tgtBusy) return;
    tgtBusy = true;
    tgtMsg = null;
    const target = tgtPath.trim();
    try {
      const r = await targetedScan(targeting.id, { path: target, recursive: tgtRecursive });
      tgtMsg = {
        ok: true,
        text: r.coalesced
          ? `Already scanning "${target || "whole library"}" — joined it.`
          : `Queued scan of "${target || "whole library"}"${r.is_file ? " (file)" : ""}.`,
      };
      // Same deferred-job race as runScan: only mark the row queued when a NEW
      // run was created. A coalesced request joined a run that is already live,
      // and the row should keep showing that run's progress.
      if (!r.coalesced) {
        queued = { ...queued, [targeting.id]: Date.now() };
        void pollUntilStarted(targeting.id);
      }
      await refresh();
    } catch (err) {
      tgtMsg = { ok: false, text: String(err) };
    } finally {
      tgtBusy = false;
    }
  }

  // create form
  let newName = $state("");
  let newPath = $state("/data/media/");
  let newNativePrefix = $state("");
  let newSharePrefix = $state("");
  // OPS-T7: deploy mount map — used to preview the auto share_prefix a new
  // library root would inherit (a placeholder/hint, not a stored value).
  let shareMap = $state<ShareMapEntry[]>([]);
  const detectedShare = $derived(
    newPath.trim() ? resolveShareHint(shareMap, newPath.trim()) : null,
  );
  // UI-T15: the detected auto share in the viewer's OS spelling (smb:// vs UNC).
  const detectedShareText = $derived(
    detectedShare
      ? formatShare(
          shareLocation(detectedShare.share_url, detectedShare.unc ?? null),
          shareFormat.pref,
          detectedPlatform,
        ) ?? detectedShare.share_url
      : null,
  );
  let showAddPicker = $state(false);
  // W8: the file taxonomy tree (category -> group -> extensions) drives both the
  // add-library gating selector and (passed down) the edit modal's. Fetched once
  // on mount; an empty tree (service offline) degrades to "all types".
  let taxonomyTree = $state<TaxonomyNode[]>([]);
  let newCategories = $state<string[]>([]);
  let newGroups = $state<string[]>([]);
  let newCron = $state("");
  let newWatch = $state(false);
  const HASH_POLICIES = ["auto", "full", "quick_only"] as const;
  let newHashPolicy = $state<import("./api").HashPolicy>("auto");
  let newHashCeiling = $state("");

  // Live scan progress, keyed by scan id, delivered over SSE. Merged over the
  // scan rows so the table shows batch counter + files/s ticking in real time.
  type Live = { status: string; seen?: number; new?: number; changed?: number;
    missing?: number; rate?: number; elapsed?: number;
    // Files enumerated but skipped, streamed live so a mis-scoped library is
    // obvious while the scan runs rather than only after it finishes.
    excluded?: number; bytes_seen?: number };
  let live = $state<Record<string, Live>>({});

  // One EventSource per actively-streamed scan id, plus reconnect backoff state.
  const streams = new Map<string, EventSource>();
  const backoff = new Map<string, number>();
  const retryTimers = new Map<string, ReturnType<typeof setTimeout>>();
  const RETRY_MIN = 1000;
  const RETRY_MAX = 15000;

  async function refresh() {
    try {
      error = "";
      const [libs, scs, st, jobs, smap] = await Promise.all([
        listLibraries(), listScans(), fetchStats(),
        failedJobs(FAILED_PAGE, failedOffset).catch(() => null),
        listShareMap().catch(() => [] as ShareMapEntry[]),
      ]);
      libraries = libs;
      shareMap = smap;
      scans = scs;
      errorCounts = (st.extract_errors as Record<string, number>) ?? {};
      if (jobs) {
        failedJobsList = jobs.items;
        failedTotal = jobs.total;
        // Clamp the offset if rows aged out / were cleared under us.
        if (failedOffset > 0 && failedOffset >= failedTotal) {
          failedOffset = Math.max(0, (Math.ceil(failedTotal / FAILED_PAGE) - 1) * FAILED_PAGE);
        }
      }
      // UI-T8: fold the fresh scan rows into `live` on EVERY refresh (including
      // the 30s safety poll) so counters advance even when all SSE streams are
      // dead. This was the frozen-UI root cause — see mergeScanRows.
      mergeScanRows();
      for (const id of Object.keys(expanded)) if (expanded[id]) await loadFailing(id);
      syncStreams();
    } catch (e) {
      error = String(e);
    }
  }

  /**
   * UI-T8 root-cause fix. `fmtStats` renders a RUNNING scan from `live[id]` in
   * preference to the DB row, so a live SSE snapshot that stops arriving (dropped
   * stream) freezes the display — the safety poll updated `scans` but the render
   * kept reading the stale `live` entry. (A full page refresh "fixed" it only
   * because it reset the empty `live` map, falling back to the fresh row.)
   *
   * Fix: whenever we (re)fetch scan rows we ALWAYS merge them into `live`, so the
   * poll alone advances the counters with SSE dead. SSE-only derived fields
   * (rate/elapsed) are preserved from any prior snapshot; an ended scan drops out
   * of `live` so its final persisted stats render straight from the row.
   */
  function mergeScanRows() {
    const merged: Record<string, Live> = { ...live };
    for (const s of scans) {
      if (s.status === "running" || s.status === "stopping") {
        const prev = merged[s.id];
        merged[s.id] = {
          ...(s.stats ?? {}),
          status: s.status,
          rate: prev?.rate,
          elapsed: prev?.elapsed,
        };
      } else if (merged[s.id]) {
        delete merged[s.id];
      }
    }
    live = merged;
  }

  /** Open SSE streams for running scans; drop streams for scans that ended. */
  function syncStreams() {
    const running = new Set(
      scans
        .filter((s) => s.status === "running" || s.status === "stopping")
        .map((s) => s.id),
    );
    for (const id of running) if (!streams.has(id)) openStream(id);
    for (const id of [...streams.keys()]) if (!running.has(id)) closeStream(id);
  }

  function closeStream(id: string) {
    streams.get(id)?.close();
    streams.delete(id);
    const t = retryTimers.get(id);
    if (t) clearTimeout(t);
    retryTimers.delete(id);
    backoff.delete(id);
  }

  function applySnapshot(id: string, d: Live) {
    // Reassign (not mutate) so Svelte's $state reactivity re-renders the row.
    live = { ...live, [id]: d };
  }

  function openStream(id: string) {
    // A prior stream/backoff timer may exist after a drop — clear before reopen.
    streams.get(id)?.close();
    const es = new EventSource(scanEventsUrl(id));
    streams.set(id, es);
    // Guards the post-close `error` the browser fires after a clean server end,
    // so we don't spuriously reconnect a scan that already finished.
    let terminated = false;

    const onData = (ev: MessageEvent) => {
      backoff.delete(id); // healthy message → reset backoff
      try {
        applySnapshot(id, JSON.parse(ev.data));
      } catch {
        /* ignore malformed frame */
      }
    };

    es.addEventListener("progress", onData);
    es.addEventListener("done", (ev) => {
      terminated = true;
      onData(ev as MessageEvent);
      closeStream(id);
      // Stream ended: one authoritative refresh so the row reflects the final
      // persisted stats (and any sidecar pass results) exactly.
      refresh();
    });
    es.addEventListener("error", (ev) => {
      if (terminated) return; // clean close already handled by `done`
      // A named server `error` frame (scan-not-found / failed-with-detail) carries
      // `.data`; a transient network drop does not. For a server frame, converge
      // via a single refresh rather than hammering reconnects.
      const serverFrame = Boolean((ev as MessageEvent).data);
      es.close();
      streams.delete(id);
      const stillRunning = scans.some(
        (s) => s.id === id && (s.status === "running" || s.status === "stopping"),
      );
      if (serverFrame || !stillRunning) {
        closeStream(id);
        refresh();
        return;
      }
      // Transient drop while the scan is still believed running: bounded backoff
      // reconnect. The safety poll (mergeScanRows) keeps counters live meanwhile.
      const delay = Math.min(backoff.get(id) ?? RETRY_MIN, RETRY_MAX);
      backoff.set(id, Math.min(delay * 2, RETRY_MAX));
      const t = setTimeout(() => openStream(id), delay);
      retryTimers.set(id, t);
    });
  }

  async function loadFailing(libId: string) {
    try {
      const res = await libraryErrors(libId);
      failing = { ...failing, [libId]: res.items };
      errorCounts = { ...errorCounts, [libId]: res.count };
    } catch (e) {
      error = String(e);
    }
  }

  async function toggleErrors(libId: string) {
    const open = !expanded[libId];
    expanded = { ...expanded, [libId]: open };
    if (open && !failing[libId]) await loadFailing(libId);
  }

  // FIX-8: failed-jobs pager + manual clear.
  async function failedPage(delta: number) {
    const next = failedOffset + delta * FAILED_PAGE;
    failedOffset = Math.max(0, Math.min(next, Math.max(0, failedTotal - 1)));
    await refresh();
  }

  async function clearFailed() {
    if (clearingFailed) return;
    if (!confirm(`Clear all ${failedTotal} failed job record(s)? This only removes the history rows — it does not affect queued or running work.`)) return;
    clearingFailed = true;
    try {
      await clearFailedJobs();
      failedOffset = 0;
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      clearingFailed = false;
    }
  }

  async function retryFailed(libId: string) {
    retrying[libId] = true;
    try {
      await retryExtracts(libId);
      await loadFailing(libId);
    } catch (e) {
      error = String(e);
    } finally {
      retrying[libId] = false;
    }
  }

  function latestScan(libId: string): ScanRun | undefined {
    return scans.find((s) => s.library_id === libId); // scans come newest-first
  }

  // Optimistic scan feedback. POST /libraries/{id}/scan only DEFERS a job — the
  // ScanRun row is created later, by the worker — so refresh() immediately after
  // the click usually still returns the PREVIOUS scan and the button looks like
  // it did nothing (the safety poll is 30s away). Record the request time on
  // click and let the row report "queued" until its own run appears.
  let queued = $state<Record<string, number>>({});

  // True from the moment a scan is requested until its ScanRun row exists. The
  // 2s slack absorbs browser/server clock skew so we still recognise our run.
  function isQueued(libId: string): boolean {
    const at = queued[libId];
    if (!at) return false;
    const s = latestScan(libId);
    if (!s) return true;
    if (Date.parse(s.started_at) >= at - 2000) return false; // our run exists
    // The request was DEDUPED into a scan that was already in flight (the API
    // coalesces rather than stacking runs). That older run is the real state —
    // show its live progress instead of a "queued" that would never clear.
    return !(s.status === "running" || s.status === "stopping");
  }

  // The worker normally picks the job up within a second or two. Poll a short
  // decaying burst so the row flips from "queued" to live progress promptly
  // rather than sitting there until the 30s safety poll. Fire-and-forget: the
  // button is already re-enabled, so this never blocks another click.
  async function pollUntilStarted(libId: string) {
    for (const delay of [800, 1500, 3000, 5000, 8000]) {
      if (!isQueued(libId)) return;
      await new Promise((r) => setTimeout(r, delay));
      await refresh();
    }
  }

  async function runScan(lib: Library) {
    busy[lib.id] = true;
    queued = { ...queued, [lib.id]: Date.now() }; // reassign, don't mutate
    try {
      await scanLibrary(lib.id);
      await refresh();
    } catch (e) {
      error = String(e);
      // The enqueue itself failed, so nothing is pending — drop the marker
      // rather than leaving a "queued" row that will never start.
      const { [lib.id]: _failed, ...rest } = queued;
      queued = rest;
      return;
    } finally {
      busy[lib.id] = false;
    }
    void pollUntilStarted(lib.id);
  }

  async function addLibrary(e: Event) {
    e.preventDefault();
    try {
      await createLibrary({
        name: newName,
        root_path: newPath,
        native_prefix: newNativePrefix.trim() || null,
        share_prefix: newSharePrefix.trim() || null,
        enabled_categories: newCategories.length ? newCategories : undefined,
        enabled_groups: newGroups.length ? newGroups : undefined,
        scan_cron: newCron.trim() || null,
        watch_mode: newWatch,
        hash_policy: newHashPolicy,
        hash_full_max_bytes: newHashCeiling.trim() ? Number(newHashCeiling) : null,
      });
      newName = "";
      newNativePrefix = "";
      newSharePrefix = "";
      newCategories = [];
      newGroups = [];
      newCron = "";
      newWatch = false;
      newHashPolicy = "auto";
      newHashCeiling = "";
      await refresh();
    } catch (err) {
      error = String(err);
    }
  }

  function fmtStats(s: ScanRun | undefined): string {
    if (!s) return "never scanned";
    const active = s.status === "running" || s.status === "stopping";
    const lv = active ? live[s.id] : undefined;
    const st: Record<string, number | undefined> = lv
      ? { seen: lv.seen, new: lv.new, changed: lv.changed, missing: lv.missing,
          excluded: lv.excluded }
      : (s.stats ?? {});
    const status = lv?.status ?? s.status;
    const inProgress = status === "running" || status === "stopping";
    const base = `${status}${s.finished_at || !inProgress ? "" : "…"}`;
    const parts = ["seen", "new", "changed", "missing", "excluded"]
      .filter((k) => st[k] !== undefined)
      .map((k) => `${k} ${st[k]}`);
    if (lv && lv.rate !== undefined) parts.push(`${lv.rate.toFixed(1)}/s`);
    return parts.length ? `${base} — ${parts.join(", ")}` : base;
  }

  // FIX-10: relative time for a persisted last-scan timestamp (compact, no deps).
  function relTime(iso: string): string {
    const secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (secs < 60) return "just now";
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    if (days < 30) return `${days}d ago`;
    const months = Math.round(days / 30);
    if (months < 12) return `${months}mo ago`;
    return `${Math.round(months / 12)}y ago`;
  }

  // FIX-10: status-badge colour for a persisted last-scan status.
  function lastScanClass(status: string): string {
    if (status === "finished") return "bg-green-500 text-white";
    if (status === "failed") return "bg-red-500 text-white";
    if (status === "stopped" || status === "cancelled") return "bg-amber-500 text-white";
    return "bg-slate-200 text-slate-600 dark:bg-slate-800 dark:text-slate-300";
  }

  // FIX-10: compact "seen N, new N" counts from a persisted last scan.
  function lastScanCounts(ls: NonNullable<Library["last_scan"]>): string {
    return (["seen", "new", "changed", "missing"] as const)
      .filter((k) => ls[k] != null)
      .map((k) => `${k} ${ls[k]}`)
      .join(", ");
  }

  // Which libraries have their exclusion breakdown panel open. A native
  // `title=` tooltip only appears after a hover delay and ignores clicks, so the
  // badge looked broken; the breakdown is now click-to-open (title kept as a
  // bonus for hover users).
  let excludedOpen = $state<Record<string, boolean>>({});

  function toggleExcluded(libId: string) {
    excludedOpen = { ...excludedOpen, [libId]: !excludedOpen[libId] };
  }

  // Structured breakdown for the click-to-open panel — one cause per line, so it
  // stays readable. excludedDetail() below flattens the same facts for `title`.
  function excludedLines(ls: NonNullable<Library["last_scan"]>): string[] {
    const out: string[] = [];
    if (ls.excluded_gate)
      out.push(`${ls.excluded_gate.toLocaleString()} rejected by this library's category/group selection.`);
    if (ls.excluded_filtered)
      out.push(`${ls.excluded_filtered.toLocaleString()} rejected by exclusion presets/globs (system files, hidden dotfiles, …).`);
    if (ls.pruned_counted && ls.pruned_files)
      out.push(`${ls.pruned_files.toLocaleString()} inside ${ls.pruned_dirs ?? 0} pruned folder(s). seen + excluded + pruned now accounts for every file on disk.`);
    else if (ls.pruned_dirs)
      out.push(`${ls.pruned_dirs} folder(s) were pruned and their contents were NOT counted, so this total is a lower bound — expect your OS to report more files. Turn on “Count files in pruned folders” in this library's settings to make it reconcile exactly.`);
    if (ls.pruned_paths?.length)
      out.push(`Pruned: ${ls.pruned_paths.slice(0, 8).join(", ")}${ls.pruned_paths.length > 8 ? ", …" : ""}`);
    if (ls.permission_denied)
      out.push(`${ls.permission_denied} folder(s) could not be read at all — contents not counted (same caveat).`);
    return out;
  }

  // Why the scan skipped files. Rendered as its own badge (not folded into
  // lastScanCounts) because it is the number that explains a gap between an OS
  // folder count and the library total — `seen` alone never could.
  function excludedDetail(ls: NonNullable<Library["last_scan"]>): string {
    const why: string[] = [];
    if (ls.excluded_gate)
      why.push(`${ls.excluded_gate} rejected by this library's category/group selection`);
    if (ls.excluded_filtered)
      why.push(`${ls.excluded_filtered} rejected by exclusion presets/globs (system files, hidden dotfiles, …)`);
    let s = why.length
      ? `${ls.excluded} file(s) enumerated but not ingested — ${why.join("; ")}.`
      : `${ls.excluded} file(s) enumerated but not ingested.`;

    // THE DISCREPANCY NOTE. Pruned/unreadable directories are skipped WITHOUT
    // being enumerated, so files inside them are counted nowhere. If the numbers
    // do not add up against the OS, this is nearly always why — so point the
    // operator straight at it, name the culprits, and name the fix.
    if (ls.pruned_counted && ls.pruned_files) {
      s += ` Plus ${ls.pruned_files.toLocaleString()} file(s) inside ${ls.pruned_dirs ?? 0} pruned director(ies), so seen + excluded + pruned = every file on disk.`;
    } else if (ls.pruned_dirs) {
      s += ` HEADS UP: ${ls.pruned_dirs} director(ies) were pruned entirely and their contents were NOT enumerated, so this count is a LOWER BOUND — expect the OS folder count to be higher.`;
      s += ` Enable "Count files in pruned folders" in this library's settings to make the numbers reconcile exactly.`;
    }
    if (ls.pruned_paths?.length)
      s += ` Pruned: ${ls.pruned_paths.slice(0, 8).join(", ")}${ls.pruned_paths.length > 8 ? ", …" : ""}.`;
    if (ls.permission_denied)
      s += ` ${ls.permission_denied} director(ies) could not be read at all (same lower-bound caveat).`;
    return s;
  }

  // After a successful edit or delete, close the modal and reconcile everything.
  function afterEdit() {
    editing = null;
    refresh();
  }
  function afterDelete() {
    deleting = null;
    refresh();
  }

  let safety: ReturnType<typeof setInterval>;
  onMount(() => {
    refresh();
    listPresets()
      .then((m) => (presetsMeta = m))
      .catch((e) => (error = String(e)));
    // W8: taxonomy for the type-gating selectors. Non-fatal — an empty tree just
    // means the selector shows "all types" and libraries can still be created.
    getTaxonomy()
      .then((t) => (taxonomyTree = t.tree))
      .catch(() => (taxonomyTree = []));
    safety = setInterval(refresh, 30000);
  });
  onDestroy(() => {
    clearInterval(safety);
    for (const id of [...streams.keys()]) closeStream(id);
  });
</script>

<div class="mt-4">
  <div class="flex items-center gap-3">
    <h2 class="text-lg font-semibold">Libraries</h2>
    <div class="grow"></div>
    <!-- W8: the taxonomy editor lives on its own route, reachable from here. -->
    <a
      href="#/taxonomy"
      class="rounded-lg border border-slate-300 px-3 py-1 text-sm text-slate-600 dark:border-slate-700 dark:text-slate-300">
      Manage file taxonomy
    </a>
  </div>

  {#if error}<p class="mt-2 text-sm text-red-500">{error}</p>{/if}

  <div class="mt-3 overflow-x-auto">
    <table class="w-full min-w-[64rem] text-sm">
      <thead>
        <tr class="border-b border-slate-200 text-left text-slate-500 dark:border-slate-800">
          <th class="py-2 pr-3 font-medium">Name</th>
          <th class="py-2 pr-3 font-medium">Root</th>
          <th class="py-2 pr-3 font-medium">Types</th>
          <th class="py-2 pr-3 font-medium">Hash</th>
          <th class="py-2 pr-3 font-medium">Cron</th>
          <th class="py-2 pr-3 font-medium">Watch</th>
          <th class="py-2 pr-3 font-medium">Errors</th>
          <th class="py-2 pr-3 font-medium">Last scan</th>
          <th class="py-2 text-right font-medium">Actions</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each libraries as lib (lib.id)}
          {@const scan = latestScan(lib.id)}
          {@const ec = errorCounts[lib.id] ?? 0}
          {@const running = scan?.status === "running"}
          {@const stopping = scan?.status === "stopping"}
          <tr class="align-top">
            <td class="py-2 pr-3 font-medium">
              {lib.name}
              {#if !lib.enabled}<span class="ml-1 rounded bg-slate-200 px-1 text-[10px] text-slate-500 dark:bg-slate-800">off</span>{/if}
            </td>
            <td class="max-w-[16rem] truncate py-2 pr-3 font-mono text-xs text-slate-500" title={lib.root_path}>
              {lib.root_path}
            </td>
            <td class="py-2 pr-3">
              <span class="inline-flex flex-wrap gap-1">
                {#if lib.enabled_categories?.length || lib.enabled_groups?.length}
                  {#each lib.enabled_categories ?? [] as c}
                    <span class="rounded bg-[var(--accent)]/15 px-1.5 py-0.5 text-[10px] text-[var(--accent)]" title="category">{c}</span>
                  {/each}
                  {#each lib.enabled_groups ?? [] as g}
                    <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-300" title="group">{g}</span>
                  {/each}
                {:else}
                  <span class="text-xs text-slate-400">all</span>
                {/if}
              </span>
            </td>
            <td class="py-2 pr-3 text-xs text-slate-500">
              {lib.hash_policy === "quick_only" ? "quick only" : lib.hash_policy}
            </td>
            <td class="py-2 pr-3 font-mono text-xs text-slate-500">{lib.scan_cron ?? "—"}</td>
            <td class="py-2 pr-3 text-xs text-slate-500">{lib.watch_mode ? "on" : "—"}</td>
            <!-- nowrap: the count badge + retry button previously stacked, which
                 alone made the whole row two lines tall. -->
            <td class="whitespace-nowrap py-2 pr-3">
              <button
                class="rounded-full px-2 py-0.5 text-xs font-medium {ec > 0 ? 'bg-red-500 text-white' : 'bg-slate-200 text-slate-500 dark:bg-slate-800'}"
                title={ec > 0 ? "Show failing items" : "No extraction errors"}
                onclick={() => toggleErrors(lib.id)}>
                {ec}
              </button>
              {#if ec > 0}
                <button
                  class="ml-1 rounded border border-slate-300 px-2 py-0.5 text-xs text-slate-600 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300"
                  title="Clear errors and re-extract the failing items"
                  disabled={retrying[lib.id]}
                  onclick={() => retryFailed(lib.id)}>
                  {retrying[lib.id] ? "…" : "retry"}
                </button>
              {/if}
            </td>
            <!-- nowrap keeps status + age + counts + the excluded badge on ONE
                 line (there is ample width before the Actions column); the
                 expanded breakdown below re-enables wrapping. -->
            <td class="whitespace-nowrap py-2 pr-3 text-xs text-slate-500">
              <!-- FIX-10: a running/stopping scan is streamed live; otherwise the
                   authoritative last scan comes from lib.last_scan (scan_runs
                   DISTINCT ON), which survives redeploys. "never" only when null. -->
              {#if isQueued(lib.id)}
                <!-- Optimistic: the job is deferred but the worker has not
                     created its ScanRun yet. Without this the click produced no
                     visible change for up to 30s. -->
                <span class="inline-flex items-center gap-1 rounded-full bg-[var(--accent)] px-1.5 py-0.5 text-[10px] font-medium text-white">
                  <span class="inline-block h-1.5 w-1.5 animate-ping rounded-full bg-white"></span>
                  queued
                </span>
                <span class="ml-1 text-slate-400">waiting for a worker…</span>
              {:else if scan && (scan.status === "running" || scan.status === "stopping")}
                {fmtStats(scan)}
              {:else if lib.last_scan}
                {@const ls = lib.last_scan}
                <span class="rounded-full px-1.5 py-0.5 text-[10px] font-medium {lastScanClass(ls.status)}">{ls.status}</span>
                <span class="ml-1" title={new Date(ls.finished_at ?? ls.started_at).toLocaleString()}>{relTime(ls.finished_at ?? ls.started_at)}</span>
                {#if lastScanCounts(ls)}<span class="ml-1 text-slate-400">— {lastScanCounts(ls)}</span>{/if}
                <!-- Render when anything was skipped, INCLUDING the pruned-only
                     case (excluded 0 but whole trees dropped) — that case is
                     exactly the one that silently loses tens of thousands of
                     files. The trailing "+" marks a lower-bound count. -->
                {#if ls.excluded || ls.pruned_dirs}
                  {@const lowerBound = !!ls.pruned_dirs && !ls.pruned_counted}
                  <button
                    type="button"
                    class="ml-1 cursor-pointer text-amber-600 underline decoration-dotted underline-offset-2 hover:decoration-solid dark:text-amber-400"
                    title={excludedDetail(ls)}
                    aria-expanded={!!excludedOpen[lib.id]}
                    onclick={() => toggleExcluded(lib.id)}
                  >excluded {(ls.excluded ?? 0).toLocaleString()}{lowerBound
                      ? "+"
                      : ls.pruned_files
                        ? ` + ${ls.pruned_files.toLocaleString()} pruned`
                        : ""}
                    <span class="text-[9px]">{excludedOpen[lib.id] ? "▲" : "▼"}</span>
                  </button>
                  {#if excludedOpen[lib.id]}
                    <ul class="mt-1 max-w-xl list-disc space-y-1 whitespace-normal rounded border border-amber-300/60 bg-amber-50 py-2 pl-6 pr-2 text-[11px] leading-snug text-slate-600 dark:border-amber-800/60 dark:bg-amber-950/30 dark:text-slate-300">
                      {#each excludedLines(ls) as line}
                        <li>{line}</li>
                      {/each}
                    </ul>
                  {/if}
                {/if}
              {:else}
                never scanned
              {/if}
            </td>
            <td class="py-2">
              <div class="flex flex-wrap items-center justify-end gap-1">
                {#if running}
                  <button
                    class="rounded-lg border border-amber-400 px-2 py-1 text-xs text-amber-600 dark:text-amber-400"
                    title="Finish the current batch and keep everything scanned so far, then end the scan. Skips deletion detection (unvisited files are NOT marked missing). The next scan picks up where this left off."
                    onclick={async () => { await stopScan(scan.id).catch(() => {}); refresh(); }}>
                    Stop (keep progress)
                  </button>
                  <button
                    class="rounded-lg border border-red-400 px-2 py-1 text-xs text-red-500"
                    title="Abort the scan immediately, discarding the in-flight batch. Use Stop to keep what was already scanned."
                    onclick={async () => { await cancelScan(scan.id).catch(() => {}); refresh(); }}>
                    Cancel (abort)
                  </button>
                {:else if stopping}
                  <span
                    class="rounded-lg border border-amber-300 px-2 py-1 text-xs text-amber-600 dark:text-amber-400"
                    title="Stop requested — finishing the current batch and wrap-up, then the scan ends as stopped.">
                    stopping…
                  </span>
                  <!-- FIX-15: escape hatch for a scan wedged in 'stopping' (worker
                       died / stop never observed). Drives it terminal; the server
                       refuses (409) if a live worker is still draining it. -->
                  <button
                    class="rounded-lg border border-red-400 px-2 py-1 text-xs text-red-500"
                    title="Force this stuck scan to a terminal state. Use only if it has been 'stopping' for a while with no progress (its worker likely died). Refused if a live worker is still processing it."
                    onclick={async () => {
                      if (!scan) return;
                      await forceClearScan(scan.id).catch(() => {});
                      refresh();
                    }}>
                    Force clear
                  </button>
                {/if}
                <button
                  class="rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300"
                  onclick={() => gotoBrowse(lib.id, "")}>Browse</button>
                <button
                  class="rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300"
                  title="Rescan a specific file or folder under this library (no full scan)"
                  onclick={() => openTargeted(lib)}>Targeted…</button>
                <button
                  class="rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300"
                  onclick={() => (editing = lib)}>Edit</button>
                <button
                  class="rounded-lg border border-red-300 px-2 py-1 text-xs text-red-500 dark:border-red-800"
                  onclick={() => (deleting = lib)}>Delete</button>
                <button
                  class="rounded-lg bg-[var(--accent)] px-2 py-1 text-xs text-white disabled:opacity-50"
                  disabled={busy[lib.id]}
                  onclick={() => runScan(lib)}>
                  {#if busy[lib.id]}
                    Starting…
                  {:else if isQueued(lib.id)}
                    Queued
                  {:else if running || stopping}
                    Restart
                  {:else}
                    Scan
                  {/if}
                </button>
              </div>
            </td>
          </tr>
          {#if expanded[lib.id]}
            <tr>
              <td colspan="9" class="bg-slate-50 px-3 py-2 text-xs dark:bg-slate-900/40">
                {#if (failing[lib.id]?.length ?? 0) === 0}
                  <span class="text-slate-500">No failing items.</span>
                {:else}
                  <ul class="space-y-1">
                    {#each failing[lib.id] as f (f.id)}
                      <li class="flex gap-2">
                        <span class="font-mono text-slate-600 dark:text-slate-300">{f.rel_path}</span>
                        <span class="text-red-500">— {f.error}</span>
                      </li>
                    {/each}
                  </ul>
                {/if}
              </td>
            </tr>
          {/if}
        {:else}
          <tr><td colspan="9" class="py-4 text-slate-500">No libraries yet — add one below.</td></tr>
        {/each}
      </tbody>
    </table>
  </div>

  <h2 class="mt-8 text-lg font-semibold">Add library</h2>
  <form class="mt-3 flex max-w-xl flex-col gap-3" onsubmit={addLibrary}>
    <label class="flex items-center gap-1 text-xs text-slate-500">Name <Help text={HELP.name} label="name" /></label>
    <input class="-mt-2 rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
      placeholder="Name" bind:value={newName} required />

    <label class="flex items-center gap-1 text-xs text-slate-500">Root path <Help text={HELP.root_path} label="root path" /></label>
    <div class="-mt-2 flex gap-2">
      <input class="grow rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono dark:border-slate-700"
        placeholder="Root path (e.g. /data/media/media)" bind:value={newPath} required />
      <button type="button" class="rounded-lg border border-slate-300 px-3 py-2 text-xs dark:border-slate-700"
        onclick={() => (showAddPicker = true)}>Browse…</button>
    </div>

    <label class="flex items-center gap-1 text-xs text-slate-500">Native prefix <Help text={HELP.native_prefix} label="native prefix" /></label>
    <input class="-mt-2 rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono dark:border-slate-700"
      placeholder="(optional) source-system prefix, e.g. /mnt/user/media" bind:value={newNativePrefix} />

    <label class="flex items-center gap-1 text-xs text-slate-500">Share location <Help text={HELP.share_prefix} label="share location" /></label>
    <input class="-mt-2 rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono dark:border-slate-700"
      placeholder={detectedShare
        ? `auto from mount: ${detectedShare.share_url}`
        : "(optional) e.g. \\tower\media, smb://tower/media, /Volumes/media"}
      bind:value={newSharePrefix} />
    {#if detectedShare && !newSharePrefix.trim()}
      <p class="-mt-1 text-xs text-slate-500">
        Auto-detected from the deploy mount map:
        <span class="font-mono text-[var(--accent)]">{detectedShare.share_url}</span>.
        Leave blank to use it, or type a value to override.
      </p>
    {/if}

    <label class="flex items-center gap-1 text-xs text-slate-500">File types <Help text={HELP.media_types} label="file types" /></label>
    <div class="-mt-2">
      <TaxonomySelector tree={taxonomyTree} bind:categories={newCategories} bind:groups={newGroups} />
    </div>

    <div class="flex flex-col gap-2">
      <label class="flex items-center gap-1 text-xs text-slate-500">Schedule <Help text={HELP.scan_cron} label="scan schedule" /></label>
      <ScheduleField value={newCron || null} onChange={(c) => (newCron = c ?? "")} />
      <label class="mt-1 inline-flex items-center gap-2 text-sm">
        <input type="checkbox" bind:checked={newWatch} />
        watch mode <Help text={HELP.watch_mode} label="watch mode" />
      </label>
    </div>

    <div class="flex flex-wrap items-center gap-3">
      <label class="inline-flex items-center gap-2 text-sm">
        hashing <Help text={HELP.hash_policy} label="hash policy" />
        <select class="rounded-lg border border-slate-300 bg-transparent px-2 py-2 dark:border-slate-700"
          bind:value={newHashPolicy}>
          {#each HASH_POLICIES as hp}
            <option value={hp}>{hp === "quick_only" ? "quick only" : hp}</option>
          {/each}
        </select>
      </label>
      <label class="flex items-center gap-1 text-xs text-slate-500">ceiling <Help text={HELP.hash_ceiling} label="hash ceiling" /></label>
      <input class="w-56 rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
        type="number" min="1"
        placeholder="full-hash byte ceiling (blank = global)"
        bind:value={newHashCeiling} />
    </div>
    <p class="-mt-1 text-xs text-slate-500">
      auto: network mounts (SMB/NFS) skip full content hashing; local paths hash in full.
    </p>
    <button class="self-start rounded-lg bg-[var(--accent)] px-4 py-2 text-white">Add library</button>
  </form>

  <CustomFieldsPanel {libraries} />

  <RbacPanel />

  {#if isAdmin}
    <UsersPanel />
  {/if}

  {#if me}
    <SessionsPanel {isAdmin} />
  {/if}

  {#if isAdmin}
    <AuditPanel />
  {/if}

  <h2 class="mt-8 text-lg font-semibold">Recent scans</h2>
  <ul class="mt-2 divide-y divide-slate-200 text-sm dark:divide-slate-800">
    {#each scans.slice(0, 10) as s (s.id)}
      {@const lib = libraries.find((l) => l.id === s.library_id)}
      <li class="flex items-center gap-3 py-2">
        <span class="rounded bg-slate-200 px-2 py-0.5 text-xs dark:bg-slate-800">{s.status}</span>
        <span class="font-medium">{lib?.name ?? s.library_id}</span>
        <span class="text-slate-500">{new Date(s.started_at).toLocaleString()}</span>
        <span class="grow"></span>
        <span class="text-xs text-slate-500">{fmtStats(s)}</span>
      </li>
    {/each}
  </ul>

  {#if failedTotal > 0}
    <div class="mt-8 flex items-center gap-3">
      <h2 class="text-lg font-semibold">Failed jobs</h2>
      <span class="text-xs text-slate-500">{failedTotal} total</span>
      <div class="grow"></div>
      <button
        class="rounded-lg border border-red-300 px-3 py-1 text-sm text-red-600 disabled:opacity-50 dark:border-red-800 dark:text-red-400"
        onclick={clearFailed}
        disabled={clearingFailed}
        title="Delete every failed job history row now. Does not affect queued or running work; failed rows also age out automatically on retention.">
        {clearingFailed ? "Clearing…" : "Clear failed history"}</button>
    </div>
    <table class="mt-2 w-full text-sm">
      <thead>
        <tr class="text-left text-slate-500">
          <th class="py-2 pr-3">Queue</th>
          <th class="py-2 pr-3">Task</th>
          <th class="py-2 pr-3">Attempts</th>
          <th class="py-2 pr-3">Last attempt</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each failedJobsList as j (j.id)}
          <tr>
            <td class="py-2 pr-3 text-slate-500">{j.queue}</td>
            <td class="py-2 pr-3 font-mono text-xs">{j.task}</td>
            <td class="py-2 pr-3 text-slate-500">{j.attempts ?? "—"}</td>
            <td class="py-2 pr-3 text-slate-500">
              {j.attempted_at ? new Date(j.attempted_at).toLocaleString() : "—"}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    <div class="mt-2 flex items-center gap-3 text-xs text-slate-500">
      <span>
        {failedOffset + 1}–{Math.min(failedOffset + FAILED_PAGE, failedTotal)} of {failedTotal}
      </span>
      <button
        class="rounded border border-slate-300 px-2 py-0.5 disabled:opacity-40 dark:border-slate-700"
        onclick={() => failedPage(-1)}
        disabled={failedOffset === 0}>Prev</button>
      <button
        class="rounded border border-slate-300 px-2 py-0.5 disabled:opacity-40 dark:border-slate-700"
        onclick={() => failedPage(1)}
        disabled={failedOffset + FAILED_PAGE >= failedTotal}>Next</button>
      <span class="grow"></span>
      <span>Error text isn't stored in the queue DB — check worker logs for tracebacks.</span>
    </div>
  {/if}
</div>

{#if editing}
  <LibraryEditModal
    library={editing}
    {presetsMeta}
    {taxonomyTree}
    onSaved={afterEdit}
    onClose={() => (editing = null)}
  />
{/if}

{#if deleting}
  <DeleteLibraryDialog
    library={deleting}
    onDeleted={afterDelete}
    onClose={() => (deleting = null)}
  />
{/if}

{#if showAddPicker}
  <FolderPicker
    initial={newPath}
    onPick={(p) => { newPath = p; showAddPicker = false; }}
    onClose={() => (showAddPicker = false)}
  />
{/if}

{#if targeting}
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
    role="button"
    tabindex="-1"
    onclick={(e) => { if (e.target === e.currentTarget) targeting = null; }}
    onkeydown={(e) => { if (e.key === "Escape") targeting = null; }}>
    <div class="w-full max-w-md rounded-xl border border-slate-200 bg-white p-4 shadow-xl dark:border-slate-800 dark:bg-slate-900">
      <h3 class="text-base font-semibold">Targeted scan — {targeting.name}</h3>
      <p class="mt-1 text-xs text-slate-500">
        Rescan one file or folder under this library. The path need not be in the
        catalog yet, but must exist on disk (e.g. a freshly created folder).
      </p>
      <form class="mt-3 flex flex-col gap-3" onsubmit={submitTargeted}>
        <label class="flex flex-col gap-1 text-xs text-slate-500">
          Path under the library root (blank = whole library)
          <input
            class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-sm dark:border-slate-700"
            placeholder="e.g. Movies/Incoming or Movies/Incoming/x.mkv"
            bind:value={tgtPath} />
        </label>
        <label class="flex items-center gap-2 text-sm" title="Ignored when the path is a file">
          <input type="checkbox" bind:checked={tgtRecursive} />
          Include subfolders
        </label>
        {#if tgtMsg}
          <p class="text-xs {tgtMsg.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500'}">
            {tgtMsg.text}
          </p>
        {/if}
        <div class="flex justify-end gap-2">
          <button
            type="button"
            class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
            onclick={() => (targeting = null)}>Close</button>
          <button
            type="submit"
            disabled={tgtBusy}
            class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm font-medium text-white disabled:opacity-50">
            {tgtBusy ? "Queueing…" : "Scan"}
          </button>
        </div>
      </form>
    </div>
  </div>
{/if}
