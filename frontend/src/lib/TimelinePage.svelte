<script lang="ts">
  import { onMount } from "svelte";
  import {
    timeline,
    listLibraries,
    type TimelineResponse,
    type TimelineBucket,
    type Library,
  } from "./api";
  import { encodeSearchHash } from "./searchparams";

  // ------------------------------------------------------------------ //
  // P3-T14 — timeline browsing. A date histogram over item mtime (month //
  // or year), rendered as clickable bars (no chart dependency). Clicking //
  // a bar applies an mtime range filter in Search; the "invalid dates"  //
  // bar (future-suspect mtimes, FIX-3) filters mtime beyond +48h.       //
  // ------------------------------------------------------------------ //

  let bucket = $state<"month" | "year">("month");
  let library = $state("");
  let libs = $state<Library[]>([]);
  let data = $state<TimelineResponse | null>(null);
  let error = $state("");
  let loading = $state(false);

  const maxCount = $derived(
    data ? Math.max(1, ...data.buckets.map((b) => b.count), data.invalid_count) : 1,
  );

  async function load() {
    error = "";
    loading = true;
    try {
      data = await timeline(bucket, library);
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  function label(b: TimelineBucket): string {
    const d = new Date(b.start);
    return bucket === "year"
      ? String(d.getUTCFullYear())
      : d.toLocaleString(undefined, { timeZone: "UTC", year: "numeric", month: "short" });
  }

  // Clicking a bucket bar navigates to Search with the bucket's [start, end)
  // mtime window (end is exclusive, so lte = end_epoch - 1 inclusive).
  function openBucket(b: TimelineBucket) {
    const params: Record<string, string> = {
      mtime_gte: String(b.start_epoch),
      mtime_lte: String(b.end_epoch - 1),
    };
    if (library) params.library = library;
    location.hash = encodeSearchHash(params);
  }

  // The "invalid dates" bar: items whose mtime is more than 48h in the future.
  function openInvalid() {
    if (!data) return;
    const params: Record<string, string> = { mtime_gte: String(data.invalid_mtime_gte) };
    if (library) params.library = library;
    location.hash = encodeSearchHash(params);
  }

  function barHeight(count: number): number {
    // Linear scale to a 160px track, with a small floor so a 1-count bar is
    // still visible/clickable.
    return Math.max(4, Math.round((count / maxCount) * 160));
  }

  onMount(async () => {
    try {
      libs = await listLibraries();
    } catch {
      // Non-fatal: the library filter just stays empty (all libraries).
    }
    load();
  });
</script>

<div>
  <div class="flex flex-wrap items-center gap-3">
    <h2 class="text-lg font-semibold">Timeline</h2>
    <span class="grow"></span>
    <label class="flex items-center gap-1 text-sm text-slate-500">
      Library
      <select
        class="rounded border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
        bind:value={library} onchange={load} aria-label="Filter by library">
        <option value="">All libraries</option>
        {#each libs as l (l.id)}<option value={l.id}>{l.name}</option>{/each}
      </select>
    </label>
    <div class="flex gap-1">
      {#each ["month", "year"] as b (b)}
        <button
          class="rounded-lg px-3 py-1 text-sm {bucket === b
            ? 'bg-[var(--accent)] text-white'
            : 'border border-slate-300 text-slate-500 dark:border-slate-700'}"
          onclick={() => { bucket = b as "month" | "year"; load(); }}>{b}</button>
      {/each}
    </div>
  </div>

  {#if error}
    <p class="mt-6 text-red-500">{error}</p>
  {:else if loading && !data}
    <p class="mt-6 text-slate-500">Loading…</p>
  {:else if data && !data.buckets.length && !data.invalid_count}
    <p class="mt-6 text-slate-500">No dated items to chart yet.</p>
  {:else if data}
    <p class="mt-4 text-sm text-slate-500">
      Click a bar to filter Search by that period.
    </p>
    <!-- Bars: a horizontally-scrolling row of clickable columns. Each bar's
         height is proportional to its item count; labels sit beneath. -->
    <div class="mt-3 overflow-x-auto rounded-lg border border-slate-200 p-4 dark:border-slate-800">
      <div class="flex items-end gap-1" style="min-height: 200px;">
        {#each data.buckets as b (b.start_epoch)}
          <button
            type="button"
            class="group flex shrink-0 flex-col items-center justify-end"
            style="width: 32px;"
            title={`${label(b)}: ${b.count} item${b.count === 1 ? "" : "s"}`}
            onclick={() => openBucket(b)}>
            <span class="mb-1 text-[10px] tabular-nums text-slate-400 group-hover:text-[var(--accent)]"
              >{b.count}</span>
            <div
              class="w-5 rounded-t bg-[var(--accent)]/60 group-hover:bg-[var(--accent)]"
              style={`height: ${barHeight(b.count)}px;`}></div>
            <span class="mt-1 w-8 -rotate-45 origin-top-left text-[10px] whitespace-nowrap text-slate-500"
              >{label(b)}</span>
          </button>
        {/each}
        {#if data.invalid_count}
          <!-- Future-suspect (invalid) mtimes, surfaced as a separate red bar. -->
          <button
            type="button"
            class="group ml-4 flex shrink-0 flex-col items-center justify-end"
            style="width: 40px;"
            title={`${data.invalid_count} item(s) with a suspect future date`}
            onclick={openInvalid}>
            <span class="mb-1 text-[10px] tabular-nums text-red-400">{data.invalid_count}</span>
            <div
              class="w-6 rounded-t bg-red-500/50 group-hover:bg-red-500"
              style={`height: ${barHeight(data.invalid_count)}px;`}></div>
            <span class="mt-1 text-[10px] text-red-500">invalid</span>
          </button>
        {/if}
      </div>
    </div>
  {/if}
</div>
