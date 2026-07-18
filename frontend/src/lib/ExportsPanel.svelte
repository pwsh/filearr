<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import {
    listExports,
    downloadExport,
    type ReportExport,
  } from "./api";

  // P11-T5/T11 — background export jobs: a status list + download. Populated by
  // scheduled deliveries and by "Queue background export" from a report. Polls
  // while any export is still queued/running so a finished job flips to
  // downloadable without a manual refresh.
  let exports = $state<ReportExport[]>([]);
  let error = $state("");
  let loading = $state(false);
  let timer: ReturnType<typeof setInterval> | null = null;

  const anyRunning = $derived(
    exports.some((e) => e.status === "queued" || e.status === "running"),
  );

  function fmtBytes(b: number | null): string {
    if (b == null || b <= 0) return "—";
    const u = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
    return `${(b / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  async function refresh() {
    loading = true;
    try {
      exports = await listExports();
      error = "";
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  async function download(ex: ReportExport) {
    try {
      await downloadExport(ex);
    } catch (e) {
      error = String(e);
    }
  }

  onMount(() => {
    refresh();
    timer = setInterval(() => {
      if (anyRunning) refresh();
    }, 4000);
  });
  onDestroy(() => timer && clearInterval(timer));
</script>

<section class="mt-8">
  <div class="mb-3 flex items-center gap-3">
    <h2 class="text-lg font-semibold">Exports</h2>
    <button
      class="rounded-lg border border-slate-300 px-2.5 py-1 text-xs disabled:opacity-50 dark:border-slate-700"
      onclick={refresh}
      disabled={loading}>Refresh</button>
  </div>

  {#if error}
    <p class="mb-3 rounded-lg bg-red-100 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-200">
      {error}
    </p>
  {/if}

  {#if exports.length === 0}
    <p class="text-sm text-slate-500">
      No background exports yet. Queue one from a report, or set up a schedule.
    </p>
  {:else}
    <div class="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-left dark:bg-slate-900">
          <tr>
            <th class="px-3 py-2 font-medium">Report</th>
            <th class="px-3 py-2 font-medium">Format</th>
            <th class="px-3 py-2 font-medium">Status</th>
            <th class="px-3 py-2 font-medium">Rows</th>
            <th class="px-3 py-2 font-medium">Size</th>
            <th class="px-3 py-2 font-medium">Created</th>
            <th class="px-3 py-2 font-medium"></th>
          </tr>
        </thead>
        <tbody>
          {#each exports as ex (ex.id)}
            <tr class="border-t border-slate-100 dark:border-slate-800">
              <td class="px-3 py-1.5">
                {ex.canned_report_key ?? "custom report"}
                {#if ex.triggered_by === "schedule"}
                  <span class="ml-1 text-xs text-slate-500">(scheduled)</span>
                {/if}
              </td>
              <td class="px-3 py-1.5 uppercase">{ex.format}</td>
              <td class="px-3 py-1.5">
                <span
                  class="rounded px-1.5 py-0.5 text-xs
                    {ex.status === 'complete'
                    ? 'bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-200'
                    : ex.status === 'failed'
                      ? 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-200'
                      : 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300'}">
                  {ex.status}
                </span>
                {#if ex.status === "failed" && ex.error}
                  <span class="ml-1 text-xs text-red-600" title={ex.error}>ⓘ</span>
                {/if}
                {#if ex.delivery_status}
                  <span class="ml-1 text-xs text-slate-500">· {ex.delivery_status}</span>
                {/if}
              </td>
              <td class="px-3 py-1.5 tabular-nums">{ex.row_count ?? "—"}</td>
              <td class="px-3 py-1.5 tabular-nums">{fmtBytes(ex.file_size_bytes)}</td>
              <td class="px-3 py-1.5 text-xs text-slate-500">
                {ex.created_at ? new Date(ex.created_at).toLocaleString() : ""}
              </td>
              <td class="px-3 py-1 text-right">
                <button
                  class="rounded border border-slate-300 px-2 py-0.5 text-xs text-[var(--accent)] disabled:opacity-40 dark:border-slate-700"
                  onclick={() => download(ex)}
                  disabled={!ex.downloadable}>
                  {ex.purged_at ? "Expired" : "Download"}
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
