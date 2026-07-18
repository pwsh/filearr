<script lang="ts">
  import { onMount } from "svelte";
  import CustomReportsPage from "./CustomReportsPage.svelte";
  import ExportsPanel from "./ExportsPanel.svelte";
  import ReportSchedulesPanel from "./ReportSchedulesPanel.svelte";
  import ItemDetail from "./ItemDetail.svelte";
  import { encodeSearchHash } from "./searchparams";
  import {
    listReports,
    runReport,
    downloadReport,
    enqueueReportExport,
    listLibraries,
    EXPORT_FORMATS,
    type ExportFormat,
    type ReportMeta,
    type ReportPage,
    type Library,
  } from "./api";

  // ------------------------------------------------------------------ //
  // P11 reporting v1 + polish — pick a canned report, run it (paginated  //
  // table), optionally filter by library, and export the full result as  //
  // CSV / NDJSON / XML. Rows are interactive per the report's row_link:   //
  // per-item rows open the ItemDetail modal (like a search hit); an       //
  // aggregate extension/hash row deep-links into a pre-filtered search.   //
  // ------------------------------------------------------------------ //

  let reports = $state<ReportMeta[]>([]);
  let libs = $state<Library[]>([]);
  let selected = $state<ReportMeta | null>(null);
  let library = $state("");
  let page = $state<ReportPage | null>(null);
  let offset = $state(0);
  let error = $state("");
  let loading = $state(false);
  let downloading = $state(false);
  let downloadOpen = $state(false);
  let showAllColumns = $state(false);
  let detailId = $state<string | null>(null); // open ItemDetail id
  let queued = $state(""); // background-export queued notice

  const pageSize = $derived(selected ? selected.default_limit : 100);

  // Bytes-ish columns get human formatting; everything else prints as text.
  const BYTE_COLUMNS = new Set(["size", "total_bytes", "wasted_bytes"]);
  // Wide path-context columns: present in every export, hidden in the table by
  // default behind a toggle (keep the table readable — library + rel_path).
  const WIDE_COLUMNS = new Set(["path", "native_path", "share_url"]);

  const visibleColumns = $derived(
    page
      ? page.columns.filter((c) => showAllColumns || !WIDE_COLUMNS.has(c))
      : [],
  );
  const hasWideColumns = $derived(
    !!page && page.columns.some((c) => WIDE_COLUMNS.has(c)),
  );

  function fmtBytes(b: number): string {
    if (!isFinite(b) || b <= 0) return "0";
    const u = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
    return `${(b / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  function cell(row: Record<string, unknown>, col: string): string {
    const v = row[col];
    if (v == null) return "";
    if (BYTE_COLUMNS.has(col) && typeof v === "number") return fmtBytes(v);
    return String(v);
  }

  // The interactive affordance for a row, driven by the report's row_link.
  function rowActionLabel(): string | null {
    if (!selected) return null;
    switch (selected.row_link) {
      case "item":
        return "Open";
      case "search_ext":
        return "Find files";
      case "search_hash":
        return "Find copies";
      default:
        return null;
    }
  }

  function rowActionDisabled(row: Record<string, unknown>): boolean {
    if (!selected) return true;
    if (selected.row_link === "item") return !row.item_id;
    if (selected.row_link === "search_ext") return !row.extension;
    if (selected.row_link === "search_hash")
      return !(row.content_hash || row.quick_hash);
    return true;
  }

  function activateRow(row: Record<string, unknown>) {
    if (!selected) return;
    if (selected.row_link === "item") {
      if (row.item_id) detailId = String(row.item_id);
    } else if (selected.row_link === "search_ext") {
      const ext = String(row.extension ?? "");
      if (ext) location.hash = encodeSearchHash({ extension: ext });
    } else if (selected.row_link === "search_hash") {
      const h = String(row.content_hash ?? row.quick_hash ?? "");
      if (h) location.hash = encodeSearchHash({ hash: h });
    }
  }

  async function select(r: ReportMeta) {
    selected = r;
    offset = 0;
    showAllColumns = false;
    if (!r.supports_library) library = "";
    await load();
  }

  async function load() {
    if (!selected) return;
    error = "";
    loading = true;
    try {
      page = await runReport(selected.id, {
        limit: pageSize,
        offset,
        libraryId: selected.supports_library ? library || undefined : undefined,
      });
    } catch (e) {
      error = String(e);
      page = null;
    } finally {
      loading = false;
    }
  }

  async function nextPage() {
    if (!page?.has_more) return;
    offset += pageSize;
    await load();
  }
  async function prevPage() {
    if (offset <= 0) return;
    offset = Math.max(0, offset - pageSize);
    await load();
  }

  async function download(format: ExportFormat) {
    if (!selected) return;
    downloadOpen = false;
    error = "";
    downloading = true;
    try {
      await downloadReport(selected.id, format, {
        limit: pageSize,
        libraryId: selected.supports_library ? library || undefined : undefined,
      });
    } catch (e) {
      error = String(e);
    } finally {
      downloading = false;
    }
  }

  async function queueBackground(format: ExportFormat) {
    if (!selected) return;
    downloadOpen = false;
    queued = "";
    error = "";
    try {
      await enqueueReportExport(selected.id, format, {
        libraryId: selected.supports_library ? library || undefined : undefined,
      });
      queued = `Queued a background ${format.toUpperCase()} export — see the Exports panel below.`;
    } catch (e) {
      error = String(e);
    }
  }

  onMount(async () => {
    try {
      [reports, libs] = await Promise.all([listReports(), listLibraries()]);
    } catch (e) {
      error = String(e);
    }
  });
</script>

<div class="grid grid-cols-1 gap-6 lg:grid-cols-[20rem_1fr]">
  <!-- registry list -->
  <aside class="space-y-2">
    <h2 class="text-lg font-semibold">Reports</h2>
    {#each reports as r (r.id)}
      <button
        class="w-full rounded-lg border px-3 py-2 text-left text-sm transition
          {selected?.id === r.id
          ? 'border-[var(--accent)] bg-[var(--accent)]/10'
          : 'border-slate-200 hover:border-slate-300 dark:border-slate-700'}"
        onclick={() => select(r)}>
        <div class="font-medium">{r.title}</div>
        <div class="mt-0.5 text-xs text-slate-500">{r.description}</div>
      </button>
    {/each}
    {#if reports.length === 0 && !error}
      <p class="text-sm text-slate-500">Loading reports…</p>
    {/if}
  </aside>

  <!-- result panel -->
  <section class="min-w-0">
    {#if error}
      <p class="mb-3 rounded-lg bg-red-100 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-200">
        {error}
      </p>
    {/if}
    {#if queued}
      <p class="mb-3 rounded-lg bg-green-100 px-3 py-2 text-sm text-green-800 dark:bg-green-950 dark:text-green-200">
        {queued}
      </p>
    {/if}

    {#if !selected}
      <p class="text-sm text-slate-500">Select a report to run it.</p>
    {:else}
      <div class="mb-3 flex flex-wrap items-center gap-3">
        <h3 class="text-base font-semibold">{selected.title}</h3>
        {#if selected.supports_library}
          <select
            class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
            bind:value={library}
            onchange={() => { offset = 0; load(); }}>
            <option value="">All libraries</option>
            {#each libs as l (l.id)}
              <option value={l.id}>{l.name}</option>
            {/each}
          </select>
        {/if}
        {#if hasWideColumns}
          <label class="inline-flex items-center gap-1.5 text-xs text-slate-500">
            <input type="checkbox" bind:checked={showAllColumns} />
            Show path columns
          </label>
        {/if}
        <div class="grow"></div>
        <!-- Download dropdown: CSV / NDJSON / XML streaming exports -->
        <div class="relative">
          <button
            class="rounded-lg border border-slate-300 px-3 py-1 text-sm disabled:opacity-50 dark:border-slate-700"
            onclick={() => (downloadOpen = !downloadOpen)}
            disabled={downloading}>
            {downloading ? "Preparing…" : "Download ▾"}
          </button>
          {#if downloadOpen}
            <div
              class="absolute right-0 z-10 mt-1 w-32 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-900">
              {#each EXPORT_FORMATS as f (f)}
                <button
                  class="block w-full px-3 py-1.5 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-800"
                  onclick={() => download(f)}>
                  {f.toUpperCase()}
                </button>
              {/each}
              <div class="border-t border-slate-200 dark:border-slate-700"></div>
              {#each EXPORT_FORMATS as f (f)}
                <button
                  class="block w-full px-3 py-1.5 text-left text-xs text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800"
                  onclick={() => queueBackground(f)}>
                  Background {f.toUpperCase()}
                </button>
              {/each}
            </div>
          {/if}
        </div>
      </div>

      {#if selected.id === "duplicate_files"}
        <p class="mb-3 rounded-lg bg-amber-100 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-200">
          A <code>quick_hash</code> tier is a <strong>sampled signal, not
          byte-verified</strong> — groups keyed on a partial (head+tail) hash may
          include files that merely share those windows. A <code>content_hash</code>
          tier is a full-hash-confirmed exact duplicate. Zero-byte files are
          excluded.
        </p>
      {/if}

      {#if loading}
        <p class="text-sm text-slate-500">Running…</p>
      {:else if page}
        <div class="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
          <table class="w-full text-sm">
            <thead class="bg-slate-50 text-left dark:bg-slate-900">
              <tr>
                {#each visibleColumns as col (col)}
                  <th class="px-3 py-2 font-medium">{col}</th>
                {/each}
                {#if rowActionLabel()}
                  <th class="px-3 py-2 font-medium"></th>
                {/if}
              </tr>
            </thead>
            <tbody>
              {#each page.rows as row, i (i)}
                <tr class="border-t border-slate-100 dark:border-slate-800">
                  {#each visibleColumns as col (col)}
                    <td class="px-3 py-1.5 align-top {BYTE_COLUMNS.has(col) ? 'tabular-nums' : ''}">
                      {cell(row, col)}
                    </td>
                  {/each}
                  {#if rowActionLabel()}
                    <td class="px-3 py-1 align-top text-right">
                      <button
                        class="rounded border border-slate-300 px-2 py-0.5 text-xs text-[var(--accent)] disabled:opacity-40 dark:border-slate-700"
                        onclick={() => activateRow(row)}
                        disabled={rowActionDisabled(row)}>
                        {rowActionLabel()}
                      </button>
                    </td>
                  {/if}
                </tr>
              {/each}
              {#if page.rows.length === 0}
                <tr>
                  <td
                    class="px-3 py-3 text-slate-500"
                    colspan={visibleColumns.length + (rowActionLabel() ? 1 : 0)}>
                    No rows.
                  </td>
                </tr>
              {/if}
            </tbody>
          </table>
        </div>

        <div class="mt-3 flex items-center gap-3 text-sm">
          <button
            class="rounded-lg border border-slate-300 px-3 py-1 disabled:opacity-40 dark:border-slate-700"
            onclick={prevPage} disabled={offset <= 0}>Previous</button>
          <span class="text-slate-500">
            rows {page.count === 0 ? 0 : offset + 1}–{offset + page.count}
          </span>
          <button
            class="rounded-lg border border-slate-300 px-3 py-1 disabled:opacity-40 dark:border-slate-700"
            onclick={nextPage} disabled={!page.has_more}>Next</button>
        </div>
      {/if}
    {/if}
  </section>
</div>

{#if detailId}
  <ItemDetail id={detailId} onClose={() => (detailId = null)} />
{/if}

<CustomReportsPage />

<ReportSchedulesPanel />

<ExportsPanel />
