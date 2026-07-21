<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import {
    clearFailedJobs,
    failedJobs,
    jobsSummary,
    reapStalledJobs,
    setJobPriority,
    type FailedJob,
    type JobsSummary,
    type ReapResult,
    type ScanRunning,
  } from "./api";
  import { help } from "./help";

  // UI-T10 — live job-system dashboard. Polls ONE composite endpoint
  // (`/system/jobs/summary`) every 4s, but ONLY while this tab is actually
  // visible (document.visibilityState) so a backgrounded tab never hammers the
  // API. All strings that originate from the filesystem (rel_path, task names)
  // render as text — Svelte auto-escapes, so a crafted path cannot inject markup.

  const POLL_MS = 4000;
  // Fixed display order for the queue cards (matches the worker's queue names).
  // `thumbs` sits after `index` (the thumbnail-creation monitor). Any OTHER queue
  // the backend reports (embed / alerts / exports / a future queue) is appended
  // dynamically by `displayQueues` so no queue silently disappears from the page.
  const QUEUES = ["scan", "extract", "index", "thumbs", "maintenance"] as const;

  let summary = $state<JobsSummary | null>(null);

  // Union of the fixed order + any extra queue present in the summary (alphabetical),
  // so embed/alerts/exports and future queues always render a card.
  let displayQueues = $derived.by<string[]>(() => {
    const fixed = QUEUES as readonly string[];
    const present = summary?.queues ? Object.keys(summary.queues) : [];
    const extras = present.filter((q) => !fixed.includes(q)).sort();
    return [...fixed, ...extras];
  });
  let error = $state("");
  let loading = $state(false);
  let lastUpdated = $state<number | null>(null);

  // FIX-8 — paginated failed-jobs list + manual clear. The failures table used
  // to render the fixed 10 rows the summary embeds (`failed_recent`); it now
  // pages the real `/system/failed-jobs` endpoint so it can never grow unbounded
  // on screen and an operator can clear the accumulated history.
  const FAILED_PAGE = 25;
  let failures = $state<FailedJob[]>([]);
  let failuresTotal = $state(0);
  let failuresOffset = $state(0);
  let clearingFailed = $state(false);

  // FIX-6 — stalled-job reaper controls. `reaping` guards the button; `reapMsg`
  // shows the last run's counts for a few seconds.
  let reaping = $state(false);
  let reapMsg = $state("");

  // UI-T14 — per-queue priority stepper. `prioInput` holds the pending edit per
  // queue (seeded from the server default the first time a queue is seen);
  // `prioBusy`/`prioMsg` drive the per-queue apply button + its result note.
  let prioInput = $state<Record<string, number>>({});
  let prioBusy = $state<Record<string, boolean>>({});
  let prioMsg = $state<Record<string, string>>({});

  // Effective input value for a queue: the user's pending edit, else the current
  // server default, else 0.
  function prioValue(q: string): number {
    if (prioInput[q] !== undefined) return prioInput[q];
    return summary?.priorities?.[q] ?? 0;
  }

  function clampPrio(n: number): number {
    if (!isFinite(n)) return 0;
    return Math.max(-100, Math.min(100, Math.round(n)));
  }

  async function applyPriority(q: string) {
    if (prioBusy[q]) return;
    const priority = clampPrio(prioValue(q));
    prioInput[q] = priority;
    prioBusy[q] = true;
    prioMsg[q] = "";
    try {
      const r = await setJobPriority(q, priority);
      prioMsg[q] =
        r.updated === 0 ? "no pending jobs" : `updated ${r.updated} pending`;
      await refresh();
    } catch (e) {
      prioMsg[q] = `failed: ${String(e)}`;
    } finally {
      prioBusy[q] = false;
    }
  }

  // Local clock so "running for" counters tick between polls (1s cadence).
  let now = $state(Date.now());

  // Rolling throughput samples per counter KEY (generalised from the original
  // extract-only tracker). Each poll appends {t, done} for a key; the rate is a
  // delta over the oldest/newest sample in the window. "—" (null) until we have
  // >=2 samples (the contract). Used for both the extract backlog and the thumbs
  // queue's completed count.
  type Sample = { t: number; done: number };
  const rateSamples: Record<string, Sample[]> = {};
  let rates = $state<Record<string, number | null>>({});

  // Cumulative-counter deltas for the I/O + network tiles: keep the previous
  // snapshot + timestamp; the displayed rate is (Δbytes / Δt) between consecutive
  // polls. First poll shows "—"; a negative delta (counter reset / device change)
  // skips that sample but still re-baselines so the next delta is correct.
  let prevIo = $state<{ read: number; write: number; t: number } | null>(null);
  let ioRate = $state<{ read: number; write: number } | null>(null);
  let prevNet = $state<{ rx: number; tx: number; t: number } | null>(null);
  let netRate = $state<{ rx: number; tx: number } | null>(null);

  // FIX-11 — human GB for the low-space banner.
  function fmtGB(bytes: number): string {
    return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  }

  // Auto-scaling binary size (B/KiB/MiB/GiB/TiB) for the disk indicator + the
  // thumbnail-cache size, so a small cache reads "820 MiB" not "0.0 GB".
  function fmtBytes(bytes: number): string {
    if (!isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let v = bytes;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return `${v.toFixed(i === 0 || v >= 100 ? 0 : 1)} ${units[i]}`;
  }

  let pollTimer: ReturnType<typeof setInterval>;
  let tickTimer: ReturnType<typeof setInterval>;

  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      error = "";
      const s = await jobsSummary();
      summary = s;
      lastUpdated = Date.now();
      recordRate("extract", s.extract.done);
      recordRate("thumbs", s.thumbs?.queue?.succeeded ?? 0);
      computeIoNet(s);
      await refreshFailures();
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  // FIX-8 — pull the current failures page. Clamps the offset back into range if
  // rows aged out (retention purge) or were cleared under us between polls.
  async function refreshFailures() {
    const page = await failedJobs(FAILED_PAGE, failuresOffset);
    failures = page.items;
    failuresTotal = page.total;
    if (failuresOffset > 0 && failuresOffset >= failuresTotal) {
      failuresOffset = Math.max(
        0,
        (Math.ceil(failuresTotal / FAILED_PAGE) - 1) * FAILED_PAGE,
      );
      const clamped = await failedJobs(FAILED_PAGE, failuresOffset);
      failures = clamped.items;
      failuresTotal = clamped.total;
    }
  }

  async function failuresPage(delta: number) {
    failuresOffset = Math.max(0, failuresOffset + delta * FAILED_PAGE);
    try {
      await refreshFailures();
    } catch (e) {
      error = String(e);
    }
  }

  async function clearFailed() {
    if (clearingFailed) return;
    if (
      !confirm(
        `Clear all ${failuresTotal} failed job record(s)? This only removes the history rows — it does not affect queued or running work.`,
      )
    )
      return;
    clearingFailed = true;
    try {
      await clearFailedJobs();
      failuresOffset = 0;
      await refresh();
    } catch (e) {
      error = `Clear failed: ${String(e)}`;
    } finally {
      clearingFailed = false;
    }
  }

  async function reap() {
    if (reaping) return;
    reaping = true;
    reapMsg = "";
    try {
      const r: ReapResult = await reapStalledJobs();
      reapMsg =
        r.reaped === 0 && r.pruned_workers === 0
          ? "Nothing stalled"
          : `Reaped ${r.reaped} (retried ${r.retried}, failed ${r.failed}), pruned ${r.pruned_workers} worker${r.pruned_workers === 1 ? "" : "s"}`;
      await refresh();
    } catch (e) {
      reapMsg = `Reap failed: ${String(e)}`;
    } finally {
      reaping = false;
    }
  }

  // Generalised rolling-rate recorder: append {t, done} for `key` and recompute
  // its per-second rate over the sample window (null with <2 samples; 0 when the
  // counter is flat/receding so a stalled queue reads "stalled").
  function recordRate(key: string, done: number) {
    const t = Date.now();
    const arr = rateSamples[key] ?? (rateSamples[key] = []);
    const prev = arr[arr.length - 1];
    arr.push({ t, done });
    if (arr.length > 6) arr.shift();
    if (arr.length >= 2 && prev) {
      const first = arr[0];
      const last = arr[arr.length - 1];
      const dt = (last.t - first.t) / 1000;
      const dd = last.done - first.done;
      rates[key] = dt > 0 && dd > 0 ? dd / dt : dd <= 0 ? 0 : null;
    } else {
      rates[key] = null;
    }
  }

  // ETA string for a queue backlog: depth / rolling-rate. "—" with <2 samples or
  // an unknown rate; "stalled" when the rate is 0; "0" when nothing is queued.
  function queueEta(key: string, depth: number): string {
    if (depth <= 0) return "0";
    const r = rates[key];
    if (r == null) return "—";
    if (r <= 0) return "stalled";
    return fmtDuration(depth / r);
  }

  // Rolling completion rate for a key, expressed per MINUTE (null with <2 samples).
  function ratePerMin(key: string): number | null {
    const r = rates[key];
    return r == null ? null : r * 60;
  }

  // Files/min for a running scan: seen ÷ elapsed since started_at. Deliberately
  // the same cumulative-average definition the SSE endpoint's `rate` uses, so
  // the Jobs page and the Admin live view never disagree. Depends on `now`
  // (1s ticker) so it keeps moving between 4s polls. Null when the backend sent
  // no started_at or under a second has passed (the divisor would explode).
  function scanFilesPerMin(s: ScanRunning): number | null {
    if (!s.started_at) return null;
    const started = Date.parse(s.started_at);
    if (Number.isNaN(started)) return null;
    const elapsedMin = (now - started) / 60000;
    if (elapsedMin <= 1 / 60) return null;
    return (s.stats.seen ?? 0) / elapsedMin;
  }

  // Compute I/O + network B/s from the cumulative counters between two polls.
  // Skips a sample on a negative delta (counter reset) but re-baselines so the
  // NEXT interval is correct; nulls the rate when the counters go away.
  function computeIoNet(s: JobsSummary) {
    const t = Date.now();
    const io = s.resources?.io ?? null;
    if (io) {
      if (prevIo) {
        const dt = (t - prevIo.t) / 1000;
        const dr = io.read_bytes - prevIo.read;
        const dw = io.write_bytes - prevIo.write;
        if (dt > 0 && dr >= 0 && dw >= 0) ioRate = { read: dr / dt, write: dw / dt };
      }
      prevIo = { read: io.read_bytes, write: io.write_bytes, t };
    } else {
      prevIo = null;
      ioRate = null;
    }
    const net = s.resources?.net ?? null;
    if (net) {
      if (prevNet) {
        const dt = (t - prevNet.t) / 1000;
        const dr = net.rx_bytes - prevNet.rx;
        const dtx = net.tx_bytes - prevNet.tx;
        if (dt > 0 && dr >= 0 && dtx >= 0) netRate = { rx: dr / dt, tx: dtx / dt };
      }
      prevNet = { rx: net.rx_bytes, tx: net.tx_bytes, t };
    } else {
      prevNet = null;
      netRate = null;
    }
  }

  // Auto-scaled byte-rate ("12.4 MiB/s").
  function fmtRate(bytesPerSec: number): string {
    return `${fmtBytes(bytesPerSec)}/s`;
  }

  // Local wall-clock HH:MM for an upcoming ISO instant.
  function hhmm(iso: string): string {
    const t = Date.parse(iso);
    if (isNaN(t)) return "";
    return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // Relative "in Xd Yh" / "in Xh Ym" / "in Xm" for an upcoming instant (uses the
  // reactive `now` clock so it ticks between polls).
  function relFuture(iso: string): string {
    const t = Date.parse(iso);
    if (isNaN(t)) return "";
    let secs = Math.max(0, (t - now) / 1000);
    const d = Math.floor(secs / 86400);
    secs -= d * 86400;
    const h = Math.floor(secs / 3600);
    secs -= h * 3600;
    const m = Math.floor(secs / 60);
    if (d > 0) return `in ${d}d ${h}h`;
    if (h > 0) return `in ${h}h ${m}m`;
    return `in ${m}m`;
  }

  function fmtDuration(secs: number): string {
    if (!isFinite(secs) || secs < 0) return "—";
    const s = Math.round(secs);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ${m % 60}m`;
    const d = Math.floor(h / 24);
    return `${d}d ${h % 24}h`;
  }

  // FIX-12 — the "attempts" column used to show procrastinate's raw per-job
  // counter. That number is honest but easily MISREAD: for extract it also counts
  // the staged-pipeline gate *reschedules* (a wait while a scan walks the same
  // library — no work done, never a failure), and legacy rows also carry pre-cap
  // stalled-job-reaper requeues. So a single file could read "attempted 137 times"
  // without ever having failed. We now pair attempts with the task's genuine-
  // failure retry budget (`retry_cap`) and split any excess out as reschedules/
  // requeues, so the number reads honestly (e.g. "2/2  +135 waiting").
  type AttemptsView = { main: string; extra: number; extraLabel: string; tooltip: string };
  function attemptsView(
    task: string,
    attempts: number | null,
    cap: number | null,
  ): AttemptsView {
    const a = attempts ?? 0;
    if (cap === null || cap === undefined) {
      // No retry configured (scan / periodic maintenance): a single failure is
      // terminal, so the raw count is already meaningful.
      return {
        main: String(a),
        extra: 0,
        extraLabel: "",
        tooltip: a === 0 ? "First run (no prior attempts)" : `${a} prior attempt${a === 1 ? "" : "s"}`,
      };
    }
    const tries = Math.min(a, cap);
    const extra = Math.max(0, a - cap);
    // Extract's excess are staged-pipeline reschedules (waits for a running scan);
    // any other task's excess (legacy pre-cap rows) are reaper requeues.
    const isExtract = task === "extract_item" || task.endsWith(".extract_item");
    const extraLabel = extra > 0 ? (isExtract ? `+${extra} waiting` : `+${extra} re-queued`) : "";
    const tooltip =
      extra > 0
        ? isExtract
          ? `${tries} of ${cap} genuine-failure retries used. The other ${extra} are staged-pipeline reschedules — the file was re-queued while a scan was running, not retried after a failure.`
          : `${tries} of ${cap} genuine-failure retries used. The other ${extra} are stalled-job-reaper re-queues (the worker died mid-job), not failures.`
        : `${tries} of ${cap} genuine-failure retries used (0 = first run).`;
    return { main: `${tries}/${cap}`, extra, extraLabel, tooltip };
  }

  // Live "running for" seconds — prefers the server-reported started_at so it
  // ticks accurately; falls back to the static seconds_running snapshot.
  function runningSecs(startedAt: string | null, snapshot: number | null): number | null {
    if (startedAt) {
      const t = Date.parse(startedAt);
      if (!isNaN(t)) return Math.max((now - t) / 1000, 0);
    }
    return snapshot;
  }

  function jobTarget(job: JobsSummary["running"][number]): string {
    if (job.rel_path) return job.rel_path;
    const a = job.args ?? {};
    if (typeof a.rel_path === "string" && a.rel_path) return a.rel_path;
    if (typeof job.library_name === "string" && job.library_name) return `library: ${job.library_name}`;
    if (typeof a.library_id === "string") return `library ${a.library_id.slice(0, 8)}`;
    if (typeof a.item_id === "string") return `item ${a.item_id.slice(0, 8)}`;
    return "—";
  }

  function isThumbTask(task: string): boolean {
    return task === "thumb_item" || task.endsWith(".thumb_item");
  }

  // Job target with a trailing file-size suffix for THUMBNAIL tasks only (size
  // predicts thumbnail-generation duration — the user's rationale). Other tasks
  // render the bare target.
  function jobTargetDisplay(job: JobsSummary["running"][number]): string {
    const base = jobTarget(job);
    if (isThumbTask(job.task) && typeof job.size === "number" && job.size > 0) {
      return `${base} (${fmtBytes(job.size)})`;
    }
    return base;
  }

  function qCount(q: string, status: string): number {
    return summary?.queues?.[q]?.[status] ?? 0;
  }

  function qStalled(q: string): number {
    return summary?.stalled?.by_queue?.[q] ?? 0;
  }

  // Inline scan progress for a running scan_library job: match the job's
  // library_id + rel_path to a running ScanRun and surface its live stats.
  function scanProgressFor(
    job: JobsSummary["running"][number],
  ): JobsSummary["scans_running"][number] | null {
    if (job.task !== "scan_library" || !summary) return null;
    const a = job.args ?? {};
    const lid = typeof a.library_id === "string" ? a.library_id : null;
    if (!lid) return null;
    const rel = typeof a.rel_path === "string" ? a.rel_path : null;
    return (
      summary.scans_running.find(
        (r) => r.library_id === lid && (r.rel_path ?? null) === rel,
      ) ?? null
    );
  }

  onMount(() => {
    refresh();
    // Poll only while the tab is the visible foreground document.
    pollTimer = setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, POLL_MS);
    // Local 1s tick for the running-for counters (cheap, no network).
    tickTimer = setInterval(() => (now = Date.now()), 1000);
    document.addEventListener("visibilitychange", onVisible);
  });

  function onVisible() {
    // Refresh immediately when the tab is brought back to the foreground.
    if (document.visibilityState === "visible") refresh();
  }

  onDestroy(() => {
    clearInterval(pollTimer);
    clearInterval(tickTimer);
    document.removeEventListener("visibilitychange", onVisible);
  });
</script>

<div class="mt-4">
  <div class="flex items-center gap-3">
    <h2 class="text-lg font-semibold">Jobs</h2>
    <span class="text-xs text-slate-500">
      {#if lastUpdated}updated {new Date(lastUpdated).toLocaleTimeString()}{:else}loading…{/if}
    </span>
    {#if summary && summary.stalled.total > 0}
      <span
        class="rounded-full bg-amber-500 px-2 py-0.5 text-xs font-medium text-white"
        title="Jobs stranded in 'doing' by a dead/restarted worker — the reaper will requeue or fail them">
        {summary.stalled.total} stalled
      </span>
    {/if}
    <div class="grow"></div>
    {#if reapMsg}<span class="text-xs text-slate-500">{reapMsg}</span>{/if}
    <button
      class="rounded-lg border border-amber-400 px-3 py-1 text-sm text-amber-700 disabled:opacity-50 dark:border-amber-600 dark:text-amber-400"
      onclick={reap}
      disabled={reaping}
      title="Requeue or fail jobs orphaned by a dead worker (also runs automatically every 5 minutes)">
      {reaping ? "Reaping…" : "Reap now"}</button>
    <button
      class="rounded-lg border border-slate-300 px-3 py-1 text-sm text-slate-600 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300"
      onclick={refresh}
      disabled={loading}>Refresh</button>
  </div>

  {#if error}<p class="mt-2 text-sm text-red-500">{error}</p>{/if}

  <!-- FIX-11 low-space banner: red at critical, amber at warn, hidden when ok.
       Data piggybacks the existing jobs-summary poll (summary.disk). -->
  {#if summary?.disk && summary.disk.status !== "ok"}
    {@const crit = summary.disk.status === "critical"}
    <div
      class="mt-3 rounded-xl border px-4 py-3 text-sm {crit
        ? 'border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200'
        : 'border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200'}"
      role="alert"
    >
      <div class="font-semibold">
        {crit ? "Critically low disk space" : "Low disk space"}
      </div>
      <p class="mt-0.5 text-xs">
        {#if crit}
          Thumbnail generation is paused (writes are refused) to protect the
          database. Free space, then reclaim via the daily/emergency thumbnail GC.
        {:else}
          Disk is running low — free space before it reaches the critical floor.
        {/if}
      </p>
      <ul class="mt-1 space-y-0.5 text-xs">
        {#each summary.disk.low as d (d.path)}
          <li>
            <span class="font-medium">{d.label}</span>
            <span class="text-current/70">({d.path})</span>:
            {fmtGB(d.free)} free of {fmtGB(d.total)}
            ({d.pct_free.toFixed(1)}%) — {d.reason}
          </li>
        {/each}
      </ul>
    </div>
  {/if}

  <!-- Resource monitors: always-on disk / CPU / I/O / network / DB tiles. All
       ride the existing jobs-summary poll (no new request). Each tile hides
       itself when its data is unavailable (older backend without `paths`, a host
       with no load average / no /proc — non-Linux dev backend — or a failed DB
       probe). -->
  {#if (summary?.disk?.paths?.length ?? 0) > 0 || summary?.resources?.cpu?.percent != null || summary?.resources?.io || summary?.resources?.net || summary?.resources?.db}
    <!-- auto-fit: every resource card shares ONE row when the viewport is wide
         enough (min 240px per card) and reflows to fewer columns as it narrows —
         no breakpoint tuning, the DB card no longer wraps alone on wide screens. -->
    <div class="mt-4 grid gap-3 grid-cols-[repeat(auto-fit,minmax(240px,1fr))]">
      {#if summary?.disk?.paths && summary.disk.paths.length > 0}
        <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
          <div class="flex items-center justify-between gap-2">
            <span class="font-medium">Disk space</span>
            <span
              class="text-xs text-slate-400"
              title="Free headroom on each filesystem the app monitors (thumbnail cache, temp dir, and — where visible — the Postgres data dir). Rows turn amber/red as they approach the low-space floors that pause thumbnail writes.">
              monitored paths
            </span>
          </div>
          <div class="mt-2 space-y-2">
            {#each summary.disk.paths as d (d.path)}
              {@const used = Math.max(0, d.total - d.free)}
              {@const usedPct = d.total > 0 ? Math.min(100, (used / d.total) * 100) : 0}
              {@const warn = d.status === "warn"}
              {@const crit = d.status === "critical"}
              <div
                class="rounded-lg px-2 py-1 {crit
                  ? 'bg-red-50 dark:bg-red-950/30'
                  : warn
                    ? 'bg-amber-50 dark:bg-amber-950/20'
                    : ''}">
                <div class="flex items-baseline justify-between gap-2 text-xs">
                  <span
                    class="truncate font-medium"
                    title={d.members && d.members.length > 1
                      ? d.members.map((m) => `${m.label}: ${m.path}`).join("\n")
                      : d.path}>{d.label}</span>
                  <span class="tabular-nums text-slate-500">
                    {fmtBytes(d.free)} free of {fmtBytes(d.total)} ({d.pct_free.toFixed(0)}%)
                  </span>
                </div>
                <div
                  class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700"
                  title="{fmtBytes(used)} used of {fmtBytes(d.total)}">
                  <div
                    class="h-full rounded-full {crit
                      ? 'bg-red-500'
                      : warn
                        ? 'bg-amber-500'
                        : 'bg-emerald-500'}"
                    style="width: {usedPct}%"></div>
                </div>
              </div>
            {/each}
          </div>
        </div>
      {/if}

      {#if summary?.resources?.cpu?.percent != null}
        {@const cpu = summary.resources.cpu}
        {@const pct = cpu.percent ?? 0}
        {@const barPct = Math.min(100, Math.max(0, pct))}
        {@const hot = pct >= 100}
        {@const warm = pct >= 80}
        <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
          <div class="flex items-center justify-between gap-2">
            <span class="font-medium">CPU load</span>
            <span
              class="tabular-nums text-xs text-slate-500"
              title="1-minute load average as a percent of available cores. Can exceed 100% when the run queue is deeper than the core count (work is waiting on CPU).">
              {pct}%
            </span>
          </div>
          <div class="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-700">
            <div
              class="h-full rounded-full {hot
                ? 'bg-red-500'
                : warm
                  ? 'bg-amber-500'
                  : 'bg-emerald-500'}"
              style="width: {barPct}%"></div>
          </div>
          <div
            class="mt-1.5 tabular-nums text-xs text-slate-500"
            title="Unix run-queue load averages over 1 / 5 / 15 minutes (os.getloadavg), and the number of cores the percent is measured against.">
            load {cpu.load1?.toFixed(2) ?? "—"} / {cpu.load5?.toFixed(2) ?? "—"} / {cpu.load15?.toFixed(2) ?? "—"}
            {#if cpu.cores != null}· {cpu.cores} core{cpu.cores === 1 ? "" : "s"}{/if}
          </div>
        </div>
      {/if}

      <!-- Combined I/O + network tile: each side alone left too much horizontal
           whitespace (two short rows per card), so disk throughput and network
           throughput share one card as side-by-side columns. B/s computed
           client-side between polls from the cumulative /proc counters; "—" on
           the first poll; a side hides itself when its counters are absent. -->
      {#if summary?.resources?.io || summary?.resources?.net}
        <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
          <div class="flex items-center justify-between gap-2">
            <span class="font-medium">I/O &amp; network</span>
            <span
              class="text-xs text-slate-400"
              title="Left: whole-disk read/write throughput across physical block devices (cumulative /proc/diskstats deltas between polls; partitions and loop/dm excluded). Right: receive/transmit summed across all interfaces except loopback (/proc/net/dev deltas).">
              throughput
            </span>
          </div>
          <div class="mt-2 grid grid-cols-2 gap-3">
            {#if summary?.resources?.io}
              <div>
                <div class="text-xs text-slate-400">disk</div>
                {#if ioRate}
                  <div class="tabular-nums text-sm text-slate-600 dark:text-slate-300">
                    {fmtRate(ioRate.read)} <span class="text-xs text-slate-400">read</span>
                  </div>
                  <div class="mt-0.5 tabular-nums text-sm text-slate-600 dark:text-slate-300">
                    {fmtRate(ioRate.write)} <span class="text-xs text-slate-400">write</span>
                  </div>
                {:else}
                  <div class="text-sm text-slate-400">—</div>
                {/if}
              </div>
            {/if}
            {#if summary?.resources?.net}
              <div>
                <div class="text-xs text-slate-400">network</div>
                {#if netRate}
                  <div class="tabular-nums text-sm text-slate-600 dark:text-slate-300">
                    {fmtRate(netRate.rx)} <span class="text-xs text-slate-400">rx</span>
                  </div>
                  <div class="mt-0.5 tabular-nums text-sm text-slate-600 dark:text-slate-300">
                    {fmtRate(netRate.tx)} <span class="text-xs text-slate-400">tx</span>
                  </div>
                {:else}
                  <div class="text-sm text-slate-400">—</div>
                {/if}
              </div>
            {/if}
          </div>
        </div>
      {/if}

      <!-- Postgres health tile. Amber highlights are INFORMATIONAL thresholds
           (documented in the tooltip), not alerts. -->
      {#if summary?.resources?.db}
        {@const db = summary.resources.db}
        {@const idleHot = db.longest_idle_in_tx_s > 60}
        {@const queryHot = db.longest_query_s > 300}
        {@const cacheHot = db.cache_hit_ratio != null && db.cache_hit_ratio < 0.9}
        <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
          <div class="flex items-center justify-between gap-2">
            <span class="font-medium">Database</span>
            <span
              class="tabular-nums text-xs text-slate-500"
              title="Live backends from pg_stat_activity for this database (active / idle-in-transaction breakdown).">
              {db.backends} conn
            </span>
          </div>
          <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
            <span class="text-slate-500">active <b class="tabular-nums text-slate-700 dark:text-slate-200">{db.active}</b></span>
            <span class="{idleHot ? 'text-amber-600 dark:text-amber-400' : 'text-slate-500'}"
              title="Backends idle inside an open transaction, and the age of the longest such session. Amber above 60s (informational — a long idle-in-transaction can hold locks / bloat).">
              idle-in-tx <b class="tabular-nums">{db.idle_in_tx}</b>
              {#if db.longest_idle_in_tx_s > 0}<span class="text-slate-400">({fmtDuration(db.longest_idle_in_tx_s)})</span>{/if}
            </span>
            {#if db.waiting > 0}
              <span class="text-slate-500">waiting <b class="tabular-nums">{db.waiting}</b></span>
            {/if}
          </div>
          <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs">
            <span class="{queryHot ? 'text-amber-600 dark:text-amber-400' : 'text-slate-500'}"
              title="Age of the longest currently-running query. Amber above 5m (informational).">
              longest query <b class="tabular-nums">{db.longest_query_s > 0 ? fmtDuration(db.longest_query_s) : "—"}</b>
            </span>
            <span class="{cacheHot ? 'text-amber-600 dark:text-amber-400' : 'text-slate-500'}"
              title="Shared-buffer cache hit ratio (blks_hit / (hit + read)). Amber below 90% (informational — a cold cache or an undersized shared_buffers).">
              cache {db.cache_hit_ratio != null ? `${(db.cache_hit_ratio * 100).toFixed(1)}%` : "—"}
            </span>
          </div>
          <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-500">
            <span>deadlocks <b class="tabular-nums {db.deadlocks > 0 ? 'text-amber-600 dark:text-amber-400' : ''}">{db.deadlocks}</b></span>
            <span title="Temporary files spilled to disk since the stats were last reset (a sign of work_mem pressure).">temp files <b class="tabular-nums">{db.temp_files}</b></span>
            <span title="Total procrastinate 'todo' backlog across all queues.">queue backlog <b class="tabular-nums">{db.queue_backlog}</b></span>
          </div>
        </div>
      {/if}
    </div>
  {/if}

  <!-- Queue summary cards -->
  <div class="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
    {#each displayQueues as q (q)}
      {@const failed = qCount(q, "failed")}
      {@const stalled = qStalled(q)}
      <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
        <div class="flex items-center justify-between gap-2">
          <span class="font-medium capitalize">{q}</span>
          <span class="flex items-center gap-1">
            {#if stalled > 0}
              <span class="rounded-full bg-amber-500 px-2 py-0.5 text-xs font-medium text-white" title="stalled doing jobs the reaper will act on">{stalled} stalled</span>
            {/if}
            {#if failed > 0}
              <span class="rounded-full bg-red-500 px-2 py-0.5 text-xs font-medium text-white">{failed} failed</span>
            {/if}
          </span>
        </div>
        {#if q === "extract"}
          <!-- Backlog depth is the headline signal for the extract queue. -->
          <div class="mt-2 flex items-baseline gap-2">
            <span class="text-3xl font-bold tabular-nums" style="color: var(--accent)">
              {summary?.extract.depth ?? 0}
            </span>
            <span class="text-xs text-slate-500">queued</span>
          </div>
          <div class="mt-1 text-xs text-slate-500">
            ETA {queueEta("extract", summary?.extract.depth ?? 0)}
            {#if rates["extract"] != null && rates["extract"] > 0}
              <span class="text-slate-400">· {rates["extract"].toFixed(1)}/s</span>
            {/if}
          </div>
          {#if summary?.staged_pipeline && summary.scans_running.length > 0}
            <!-- UI-T14: staged mode holds extraction until the running scan ends -->
            <div class="mt-1 text-xs font-medium text-amber-600 dark:text-amber-400"
              title="Staged pipeline is on: extraction is deferred until the running scan finishes walking, so the scan and extract workers don't hit disk/network at once.">
              staged — waiting for scan
            </div>
          {/if}
        {/if}
        {#if q === "thumbs" && summary?.thumbs}
          <!-- Thumbnail-creation monitor: pending headline + whole-cache totals. -->
          <div class="mt-2 flex items-baseline gap-2">
            <span class="text-3xl font-bold tabular-nums" style="color: var(--accent)">
              {qCount(q, "todo")}
            </span>
            <span class="text-xs text-slate-500">pending</span>
          </div>
          <div class="mt-1 text-xs text-slate-500"
            title="Average completion rate of the thumbnail queue over the last few polls and the resulting ETA to clear the current backlog. '—' until at least two samples are seen.">
            {#if ratePerMin("thumbs") != null}
              ~{Math.round(ratePerMin("thumbs") ?? 0)}/min · ETA {queueEta("thumbs", qCount(q, "todo"))}
            {:else}
              —
            {/if}
          </div>
          <div class="mt-1 text-xs text-slate-500"
            title="Thumbnails generated into the cache (rows in the thumbnail manifest) and the total bytes they occupy on disk. Failed generations show as the queue's failed count.">
            generated <b class="tabular-nums text-slate-700 dark:text-slate-200">{summary.thumbs.generated}</b>
            · {fmtBytes(summary.thumbs.bytes)}
          </div>
        {/if}
        {#if q === "scan" && summary?.scan_throughput}
          <!-- Aggregate walk throughput across recent FINISHED scans. Weighted
               (total files ÷ total walk seconds), so one tiny scoped rescan
               cannot skew it the way averaging per-run rates would. -->
          {@const tp = summary.scan_throughput}
          <div class="mt-2 flex items-baseline gap-2">
            <span class="text-3xl font-bold tabular-nums" style="color: var(--accent)">
              {tp.runs > 0 ? Math.round(tp.files_per_min).toLocaleString() : "—"}
            </span>
            <span class="text-xs text-slate-500">files/min avg</span>
          </div>
          <div class="mt-1 text-xs text-slate-500"
            title="Weighted average across finished scans in the window: total files walked ÷ total walk seconds. NOT the mean of each run's rate — that would let a 3-file rescan outweigh a 500k-file full scan. Only the directory walk counts; extraction runs out-of-band on the extract queue.">
            {#if tp.runs > 0}
              {tp.runs} run{tp.runs === 1 ? "" : "s"} · last {tp.window_days}d
            {:else}
              no finished scans in the last {tp.window_days}d
            {/if}
          </div>
          {#if tp.runs > 0}
            <div class="mt-1 text-xs text-slate-500"
              title="Total files walked and their total on-disk size across those runs, with the resulting throughput.">
              walked <b class="tabular-nums text-slate-700 dark:text-slate-200">{tp.files.toLocaleString()}</b>
              · {fmtBytes(tp.bytes)}{#if tp.bytes_per_s > 0} · {fmtRate(tp.bytes_per_s)}{/if}
            </div>
          {/if}
        {/if}
        <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
          <span class="text-slate-500">todo <b class="tabular-nums text-slate-700 dark:text-slate-200">{qCount(q, "todo")}</b></span>
          <span class="text-slate-500">doing <b class="tabular-nums text-slate-700 dark:text-slate-200">{qCount(q, "doing")}</b></span>
          {#if stalled > 0}
            <span class="text-amber-600 dark:text-amber-400">stalled <b class="tabular-nums">{stalled}</b></span>
          {/if}
          <span class="text-slate-500">done <b class="tabular-nums text-slate-700 dark:text-slate-200">{qCount(q, "succeeded")}</b></span>
          <span class="{failed > 0 ? 'text-red-500' : 'text-slate-500'}">failed <b class="tabular-nums">{failed}</b></span>
        </div>

        <!-- UI-T14: per-queue priority stepper (admin). Shows the current default
             and applies a new priority to this queue's PENDING (todo) jobs;
             running jobs keep the priority they were fetched with. -->
        <div class="mt-3 border-t border-slate-100 pt-2 dark:border-slate-800">
          <div class="flex items-center justify-between gap-2">
            <label class="text-xs text-slate-500" for={`prio-${q}`}>priority</label>
            <span class="text-[10px] text-slate-400">default {summary?.priorities?.[q] ?? 0}</span>
          </div>
          <div class="mt-1 flex items-center gap-1">
            <button
              class="rounded border border-slate-300 px-1.5 text-sm leading-5 text-slate-600 disabled:opacity-40 dark:border-slate-700 dark:text-slate-300"
              title="lower priority (runs later)"
              onclick={() => (prioInput[q] = clampPrio(prioValue(q) - 1))}
              disabled={prioBusy[q]}>−</button>
            <input
              id={`prio-${q}`}
              class="w-14 rounded border border-slate-300 px-1 py-0.5 text-center text-xs tabular-nums dark:border-slate-700 dark:bg-slate-900"
              type="number" min="-100" max="100"
              value={prioValue(q)}
              oninput={(e) => (prioInput[q] = clampPrio(+(e.currentTarget as HTMLInputElement).value))}
              disabled={prioBusy[q]} />
            <button
              class="rounded border border-slate-300 px-1.5 text-sm leading-5 text-slate-600 disabled:opacity-40 dark:border-slate-700 dark:text-slate-300"
              title="higher priority (runs sooner)"
              onclick={() => (prioInput[q] = clampPrio(prioValue(q) + 1))}
              disabled={prioBusy[q]}>+</button>
            <button
              class="ml-auto rounded-lg border border-slate-300 px-2 py-0.5 text-xs text-slate-600 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300"
              onclick={() => applyPriority(q)}
              disabled={prioBusy[q]}
              title="Apply this priority to the queue's pending (todo) jobs — running jobs are unaffected.">
              {prioBusy[q] ? "…" : `apply to ${qCount(q, "todo")}`}</button>
          </div>
          {#if prioMsg[q]}<div class="mt-1 text-[10px] text-slate-500">{prioMsg[q]}</div>{/if}
        </div>

        <!-- Upcoming scheduled work for this queue (≤3 soonest). Rides the same
             poll (summary.upcoming). Omitted entirely when the queue has none. -->
        {#if summary?.upcoming?.[q]?.length}
          <div class="mt-3 border-t border-slate-100 pt-2 dark:border-slate-800">
            <div class="text-[10px] font-medium uppercase tracking-wide text-slate-400">Upcoming</div>
            <ul class="mt-1 space-y-0.5">
              {#each summary.upcoming[q] as u (u.label + u.at)}
                <li
                  class="flex items-baseline justify-between gap-2 text-[11px] text-slate-500"
                  title="{u.task} — {u.label} at {new Date(u.at).toLocaleString()}">
                  <span class="truncate">{u.label}</span>
                  <span class="shrink-0 tabular-nums text-slate-400">{hhmm(u.at)} · {relFuture(u.at)}</span>
                </li>
              {/each}
            </ul>
          </div>
        {/if}
      </div>
    {/each}
  </div>
  <p class="mt-2 text-xs text-slate-500">{help("job_priority")}</p>

  <!-- Meili sync strip -->
  {#if summary?.meili}
    {@const m = summary.meili}
    <div class="mt-4 flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 px-4 py-2 text-sm dark:border-slate-800">
      <span class="font-medium">Search index</span>
      {#if !m.healthy}
        <span class="rounded-full bg-red-500 px-2 py-0.5 text-xs font-medium text-white">unreachable</span>
      {:else if m.in_sync}
        <span class="rounded-full bg-emerald-500 px-2 py-0.5 text-xs font-medium text-white">in sync</span>
      {:else}
        <span class="rounded-full bg-amber-500 px-2 py-0.5 text-xs font-medium text-white">drift {m.drift ?? "?"}</span>
      {/if}
      <span class="text-slate-500">docs <b class="tabular-nums text-slate-700 dark:text-slate-200">{m.document_count ?? "—"}</b></span>
      <span class="text-slate-500">postgres active <b class="tabular-nums text-slate-700 dark:text-slate-200">{m.postgres_active}</b></span>
      {#if m.is_indexing}
        <span class="flex items-center gap-1 text-xs text-slate-500">
          <span class="inline-block h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-[var(--accent)]"></span>
          indexing
        </span>
      {/if}
    </div>
  {/if}

  <!-- Running scans -->
  {#if summary && summary.scans_running.length > 0}
    <h3 class="mt-6 text-base font-semibold">Running scans</h3>
    <table class="mt-2 w-full text-sm">
      <thead>
        <tr class="text-left text-slate-500">
          <th class="py-2 pr-3">Library</th>
          <th class="py-2 pr-3">Scope</th>
          <th class="py-2 pr-3">Seen</th>
          <th class="py-2 pr-3">New</th>
          <th class="py-2 pr-3">Changed</th>
          <th
            class="py-2 pr-3 text-right"
            title="Files skipped: the library's category/group selection or the exclusion presets rejected them. seen + excluded = files enumerated."
          >Excluded</th>
          <th
            class="py-2 pr-3 text-right"
            title="Total on-disk size of the files walked so far."
          >Size</th>
          <th
            class="py-2 pr-3 text-right"
            title="Average since the scan started (seen ÷ elapsed), not an instantaneous rate. Seen advances in batches of 250, so short scans read as a staircase."
          >Files/min</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each summary.scans_running as s (s.id)}
          {@const fpm = scanFilesPerMin(s)}
          <tr>
            <td class="py-2 pr-3 font-medium">{s.library_name}</td>
            <td class="py-2 pr-3 font-mono text-xs text-slate-500">{s.rel_path ?? "(whole library)"}</td>
            <td class="py-2 pr-3 tabular-nums text-slate-500">{s.stats.seen ?? 0}</td>
            <td class="py-2 pr-3 tabular-nums text-slate-500">{s.stats.new ?? 0}</td>
            <td class="py-2 pr-3 tabular-nums text-slate-500">{s.stats.changed ?? 0}</td>
            <td class="py-2 pr-3 text-right tabular-nums text-slate-500">{s.stats.excluded ?? 0}</td>
            <td class="py-2 pr-3 text-right tabular-nums text-slate-500">{fmtBytes(s.stats.bytes_seen ?? 0)}</td>
            <td class="py-2 pr-3 text-right tabular-nums text-slate-500">
              {fpm === null ? "—" : Math.round(fpm).toLocaleString()}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <!-- Running now -->
  <h3 class="mt-6 text-base font-semibold">Running now</h3>
  {#if summary && summary.running.length > 0}
    <table class="mt-2 w-full text-sm">
      <thead>
        <tr class="text-left text-slate-500">
          <th class="py-2 pr-3">Task</th>
          <th class="py-2 pr-3">Queue</th>
          <th class="py-2 pr-3">Target</th>
          <th class="py-2 pr-3 text-right" title="Tries used against the task's genuine-failure retry budget (attempts/cap). Extract also re-queues while a scan runs — those waits are shown separately, not as failures.">Attempts</th>
          <th class="py-2 pr-3 text-right">Running for</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each summary.running as job (job.id)}
          {@const secs = runningSecs(job.started_at, job.seconds_running)}
          {@const prog = scanProgressFor(job)}
          {@const av = attemptsView(job.task, job.attempts, job.retry_cap)}
          <tr class={job.stalled ? "bg-amber-50/60 dark:bg-amber-950/20" : ""}>
            <td class="py-2 pr-3 font-mono text-xs">
              <span class="flex items-center gap-1.5">
                {job.task}
                {#if job.stalled}
                  <span
                    class="rounded-full bg-amber-500 px-1.5 py-0.5 text-[10px] font-medium text-white"
                    title={job.worker_id === null
                      ? "No live worker owns this job (worker restarted) — the reaper will requeue it"
                      : !job.worker_alive
                        ? "This job's worker stopped sending heartbeats — the reaper will requeue it"
                        : "Running far longer than expected — the reaper will requeue it"}>stalled</span>
                {/if}
              </span>
              {#if prog}
                {@const pfpm = scanFilesPerMin(prog)}
                <span class="mt-0.5 block text-[10px] font-normal text-slate-500">
                  seen {prog.stats.seen ?? 0} · new {prog.stats.new ?? 0} · changed {prog.stats.changed ?? 0}{#if prog.stats.excluded} · excluded {prog.stats.excluded}{/if}
                  {#if pfpm !== null}· {Math.round(pfpm).toLocaleString()}/min{/if}
                  {#if prog.stats.bytes_seen}· {fmtBytes(prog.stats.bytes_seen)}{/if}
                </span>
              {/if}
            </td>
            <td class="py-2 pr-3 text-slate-500">{job.queue}</td>
            <td class="max-w-[28rem] truncate py-2 pr-3 font-mono text-xs text-slate-500" title={jobTargetDisplay(job)}>{jobTargetDisplay(job)}</td>
            <td class="py-2 pr-3 text-right tabular-nums text-slate-500" title={av.tooltip}>
              {av.main}{#if av.extra > 0}<span class="ml-1 text-[10px] font-normal text-slate-400">{av.extraLabel}</span>{/if}
            </td>
            <td class="py-2 pr-3 text-right tabular-nums text-slate-500">{secs === null ? "—" : fmtDuration(secs)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <p class="mt-2 text-xs text-slate-500">
      Attempts shows tries used against each task's genuine-failure retry budget
      (attempts/cap). Extract re-queues itself while a scan is running — those waits
      are counted separately as “waiting”, not as failed tries, so a file is never
      retried past its cap after a real failure.
    </p>
  {:else if summary}
    <p class="mt-2 text-sm text-slate-500">No jobs are executing right now.</p>
  {/if}

  <!-- Failed jobs (FIX-8: paginated + clearable) -->
  <div class="mt-6 flex items-center gap-3">
    <h3 class="text-base font-semibold">Failed jobs</h3>
    {#if failuresTotal > 0}
      <span class="text-xs text-slate-500">{failuresTotal} total</span>
    {/if}
    <div class="grow"></div>
    {#if failuresTotal > 0}
      <button
        class="rounded-lg border border-red-300 px-3 py-1 text-sm text-red-600 disabled:opacity-50 dark:border-red-800 dark:text-red-400"
        onclick={clearFailed}
        disabled={clearingFailed}
        title="Delete every failed job history row now. Does not affect queued or running work; failed rows also age out automatically on retention.">
        {clearingFailed ? "Clearing…" : "Clear failed history"}</button>
    {/if}
  </div>
  {#if failures.length > 0}
    <table class="mt-2 w-full text-sm">
      <thead>
        <tr class="text-left text-slate-500">
          <th class="py-2 pr-3">Task</th>
          <th class="py-2 pr-3">Queue</th>
          <th class="py-2 pr-3" title="Tries used against the task's genuine-failure retry budget (attempts/cap). A large legacy number can include reschedules/requeues, not repeated failures.">Attempts</th>
          <th class="py-2 pr-3">Last attempt</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each failures as j (j.id)}
          {@const av = attemptsView(j.task, j.attempts, j.retry_cap)}
          <tr>
            <td class="py-2 pr-3 font-mono text-xs">{j.task}</td>
            <td class="py-2 pr-3 text-slate-500">{j.queue}</td>
            <td class="py-2 pr-3 tabular-nums text-slate-500" title={av.tooltip}>
              {av.main}{#if av.extra > 0}<span class="ml-1 text-[10px] font-normal text-slate-400">{av.extraLabel}</span>{/if}
            </td>
            <td class="py-2 pr-3 text-slate-500">{j.attempted_at ? new Date(j.attempted_at).toLocaleString() : "—"}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <div class="mt-2 flex items-center gap-3 text-xs text-slate-500">
      <span>
        {failuresOffset + 1}–{Math.min(failuresOffset + FAILED_PAGE, failuresTotal)} of {failuresTotal}
      </span>
      <button
        class="rounded border border-slate-300 px-2 py-0.5 disabled:opacity-40 dark:border-slate-700"
        onclick={() => failuresPage(-1)}
        disabled={failuresOffset === 0}>Prev</button>
      <button
        class="rounded border border-slate-300 px-2 py-0.5 disabled:opacity-40 dark:border-slate-700"
        onclick={() => failuresPage(1)}
        disabled={failuresOffset + FAILED_PAGE >= failuresTotal}>Next</button>
      <span class="grow"></span>
      <span>Error text isn't stored in the queue DB (a known Procrastinate limitation) — check the worker logs for tracebacks.</span>
    </div>
  {:else if summary}
    <p class="mt-2 text-sm text-slate-500">No failed jobs.</p>
  {/if}
  <p class="mt-2 text-xs text-slate-500">{help("job_history_retention")}</p>
</div>
