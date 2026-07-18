<script lang="ts">
  import { onMount } from "svelte";
  import ItemDetail from "./ItemDetail.svelte";
  import DslHelp from "./DslHelp.svelte";
  import { setBuilderPrefill, takeReportPrefill } from "./handoff";
  import {
    listCustomReports,
    getColumnRegistry,
    validateCustomReport,
    createCustomReport,
    updateCustomReport,
    deleteCustomReport,
    runCustomReport,
    downloadCustomReport,
    EXPORT_FORMATS,
    type ExportFormat,
    type ReportDefinition,
    type ColumnRegistry,
    type ReportValidationError,
    type CustomRunPage,
  } from "./api";

  // ------------------------------------------------------------------ //
  // P11 custom (saved-query) reports. A report is a querydsl string +   //
  // a column projection; the backend validates by PARSING + TRANSLATING //
  // the query on save, and /run streams the same CSV path as canned.    //
  // ------------------------------------------------------------------ //

  let defs = $state<ReportDefinition[]>([]);
  let registry = $state<ColumnRegistry | null>(null);
  let error = $state("");

  // form state
  let showForm = $state(false);
  let editingId = $state<string | null>(null);
  let fName = $state("");
  let fQuery = $state("");
  let fSelected = $state<Set<string>>(new Set(["rel_path", "library", "size"]));
  let fExtra = $state(""); // comma-separated free meta./cf. columns
  let fFormat = $state("csv");
  let vErrors = $state<ReportValidationError[]>([]);
  let vOk = $state(true);
  let saving = $state(false);

  // row-open (ItemDetail) + per-report download menu
  let detailId = $state<string | null>(null);
  let downloadMenuId = $state<string | null>(null);

  // run state
  let runId = $state<string | null>(null);
  let runName = $state("");
  let runPage = $state<CustomRunPage | null>(null);
  let runOffset = $state(0);
  let running = $state(false);

  const BYTE_COLUMNS = new Set(["size", "total_bytes", "wasted_bytes"]);
  const RUN_LIMIT = 200;

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
    return typeof v === "object" ? JSON.stringify(v) : String(v);
  }

  function formColumns(): string[] {
    const extra = fExtra
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    // preserve a stable order: registry order for selected, then free entries
    const ordered: string[] = [];
    for (const c of registryColumns()) if (fSelected.has(c)) ordered.push(c);
    for (const e of extra) if (!ordered.includes(e)) ordered.push(e);
    return ordered;
  }

  function registryColumns(): string[] {
    if (!registry) return [];
    return [...registry.core, ...registry.custom_fields.map((n) => `cf.${n}`)];
  }

  function toggleColumn(c: string) {
    const next = new Set(fSelected);
    if (next.has(c)) next.delete(c);
    else next.add(c);
    fSelected = next;
    scheduleValidate();
  }

  let validateTimer: ReturnType<typeof setTimeout> | undefined;
  function scheduleValidate() {
    clearTimeout(validateTimer);
    validateTimer = setTimeout(runValidate, 300);
  }

  // FIX-12 (Item B): a "Query syntax" help chip appends its example to the report
  // query and re-validates, teaching the filter DSL by example.
  function insertDsl(frag: string) {
    fQuery = fQuery.trim() ? `${fQuery.trim()} ${frag}` : frag;
    scheduleValidate();
  }
  async function runValidate() {
    try {
      const res = await validateCustomReport({
        query: fQuery,
        columns: formColumns(),
      });
      vOk = res.ok;
      vErrors = res.errors;
    } catch (e) {
      vOk = false;
      vErrors = [{ error: "request_error", message: String(e) }];
    }
  }

  function errText(e: ReportValidationError): string {
    if (e.error === "parse_error")
      return `Parse error (${e.code}) at position ${e.position}: ${e.reason}`;
    if (e.error === "translation_error")
      return e.unsupported && e.unsupported.length
        ? `${e.message}`
        : `${e.message}`;
    return e.message ?? e.error;
  }

  function openCreate() {
    editingId = null;
    fName = "";
    fQuery = "";
    fSelected = new Set(["rel_path", "library", "size"]);
    fExtra = "";
    fFormat = "csv";
    vErrors = [];
    vOk = true;
    showForm = true;
  }
  function openEdit(d: ReportDefinition) {
    editingId = d.id;
    fName = d.name;
    fQuery = d.query;
    const known = new Set(registryColumns());
    fSelected = new Set(d.columns.filter((c) => known.has(c)));
    fExtra = d.columns.filter((c) => !known.has(c)).join(", ");
    fFormat = d.format;
    vErrors = [];
    vOk = true;
    showForm = true;
    scheduleValidate();
  }
  function cancelForm() {
    showForm = false;
    editingId = null;
  }

  // Round-trip to the visual builder: parse this report's DSL back into rows.
  function editInBuilder(d: ReportDefinition) {
    setBuilderPrefill({ query: d.query });
    location.hash = "#/filter-builder";
  }

  async function save() {
    error = "";
    saving = true;
    try {
      const body = {
        name: fName,
        query: fQuery,
        columns: formColumns(),
        format: fFormat,
      };
      if (editingId) await updateCustomReport(editingId, body);
      else await createCustomReport(body);
      showForm = false;
      editingId = null;
      defs = await listCustomReports();
    } catch (e) {
      error = String(e);
    } finally {
      saving = false;
    }
  }

  async function remove(d: ReportDefinition) {
    if (!confirm(`Delete custom report “${d.name}”?`)) return;
    error = "";
    try {
      await deleteCustomReport(d.id);
      if (runId === d.id) {
        runId = null;
        runPage = null;
      }
      defs = await listCustomReports();
    } catch (e) {
      error = String(e);
    }
  }

  async function run(d: ReportDefinition, offset = 0) {
    error = "";
    running = true;
    runId = d.id;
    runName = d.name;
    runOffset = offset;
    try {
      runPage = await runCustomReport(d.id, { limit: RUN_LIMIT, offset });
    } catch (e) {
      error = String(e);
      runPage = null;
    } finally {
      running = false;
    }
  }

  async function download(d: ReportDefinition, format: ExportFormat) {
    downloadMenuId = null;
    error = "";
    try {
      await downloadCustomReport(d.id, d.name, format);
    } catch (e) {
      error = String(e);
    }
  }

  onMount(async () => {
    try {
      [defs, registry] = await Promise.all([
        listCustomReports(),
        getColumnRegistry(),
      ]);
    } catch (e) {
      error = String(e);
    }
    // Handoff from the filter builder ("Save as custom report"): open the create
    // form pre-filled with the compiled DSL + a sensible column projection.
    const pf = takeReportPrefill();
    if (pf) {
      openCreate();
      fQuery = pf.query;
      if (pf.name) fName = pf.name;
      if (pf.columns && pf.columns.length) {
        const known = new Set(registryColumns());
        fSelected = new Set(pf.columns.filter((c) => known.has(c)));
        fExtra = pf.columns.filter((c) => !known.has(c)).join(", ");
      }
      scheduleValidate();
    }
  });
</script>

<div class="mt-8 border-t border-slate-200 pt-6 dark:border-slate-800">
  <div class="mb-3 flex items-center gap-3">
    <h2 class="text-lg font-semibold">Custom reports</h2>
    <div class="grow"></div>
    <button
      class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white"
      onclick={openCreate}>New custom report</button>
  </div>

  {#if error}
    <p class="mb-3 rounded-lg bg-red-100 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-200">
      {error}
    </p>
  {/if}

  <!-- create / edit form -->
  {#if showForm}
    <div class="mb-5 rounded-lg border border-slate-300 p-4 dark:border-slate-700">
      <div class="grid gap-3 sm:grid-cols-2">
        <label class="text-sm">
          <span class="mb-1 block text-slate-500">Name</span>
          <input
            class="w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-700"
            bind:value={fName} placeholder="e.g. Low-res videos" />
        </label>
        <label class="text-sm">
          <span class="mb-1 block text-slate-500">Default format</span>
          <select
            class="w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-700"
            bind:value={fFormat}>
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
          </select>
        </label>
      </div>

      <label class="mt-3 block text-sm">
        <span class="mb-1 block text-slate-500">Query (filter DSL)</span>
        <input
          class="w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono dark:border-slate-700"
          bind:value={fQuery}
          oninput={scheduleValidate}
          placeholder="kind:video meta.height:>=1080 -tag:archived" />
      </label>
      <DslHelp onInsert={insertDsl} context="reports" />

      <div class="mt-3 text-sm">
        <span class="mb-1 block text-slate-500">Columns</span>
        <div class="flex flex-wrap gap-x-4 gap-y-1">
          {#each registryColumns() as c (c)}
            <label class="inline-flex items-center gap-1.5">
              <input
                type="checkbox"
                checked={fSelected.has(c)}
                onchange={() => toggleColumn(c)} />
              <span class="font-mono text-xs">{c}</span>
            </label>
          {/each}
        </div>
        <label class="mt-2 block">
          <span class="mb-1 block text-xs text-slate-500">
            Extra meta./cf. columns (comma-separated)
          </span>
          <input
            class="w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-slate-700"
            bind:value={fExtra}
            oninput={scheduleValidate}
            placeholder="meta.video_codec, meta.bitrate" />
        </label>
      </div>

      {#if vErrors.length}
        <ul class="mt-3 space-y-1">
          {#each vErrors as e, i (i)}
            <li class="rounded bg-amber-100 px-2 py-1 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-200">
              {errText(e)}
            </li>
          {/each}
        </ul>
      {:else if fQuery || formColumns().length}
        <p class="mt-3 text-xs text-emerald-600 dark:text-emerald-400">Query is valid.</p>
      {/if}

      <div class="mt-4 flex items-center gap-2">
        <button
          class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white disabled:opacity-50"
          onclick={save}
          disabled={saving || !vOk || !fName || !formColumns().length}>
          {saving ? "Saving…" : editingId ? "Save changes" : "Create"}
        </button>
        <button
          class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
          onclick={cancelForm}>Cancel</button>
      </div>
    </div>
  {/if}

  <!-- definitions list -->
  {#if defs.length === 0}
    <p class="text-sm text-slate-500">No custom reports yet.</p>
  {:else}
    <div class="space-y-2">
      {#each defs as d (d.id)}
        <div class="flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-700">
          <div class="min-w-0">
            <div class="font-medium">{d.name}</div>
            <div class="truncate font-mono text-xs text-slate-500">{d.query || "(all items)"}</div>
          </div>
          <div class="grow"></div>
          <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700" onclick={() => run(d)}>Run</button>
          <div class="relative">
            <button
              class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
              onclick={() => (downloadMenuId = downloadMenuId === d.id ? null : d.id)}>Download ▾</button>
            {#if downloadMenuId === d.id}
              <div class="absolute right-0 z-10 mt-1 w-32 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-900">
                {#each EXPORT_FORMATS as f (f)}
                  <button
                    class="block w-full px-3 py-1.5 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-800"
                    onclick={() => download(d, f)}>{f.toUpperCase()}</button>
                {/each}
              </div>
            {/if}
          </div>
          <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700" onclick={() => editInBuilder(d)}>Edit in builder</button>
          <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700" onclick={() => openEdit(d)}>Edit</button>
          <button class="rounded-lg border border-red-300 px-3 py-1 text-sm text-red-700 dark:border-red-800 dark:text-red-300" onclick={() => remove(d)}>Delete</button>
        </div>
      {/each}
    </div>
  {/if}

  <!-- run result -->
  {#if runId}
    <div class="mt-5">
      <h3 class="mb-2 text-base font-semibold">Result — {runName}</h3>
      {#if running}
        <p class="text-sm text-slate-500">Running…</p>
      {:else if runPage}
        <div class="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
          <table class="w-full text-sm">
            <thead class="bg-slate-50 text-left dark:bg-slate-900">
              <tr>
                {#each runPage.columns as col (col)}
                  <th class="px-3 py-2 font-medium">{col}</th>
                {/each}
                <th class="px-3 py-2 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {#each runPage.rows as row, i (i)}
                <tr class="border-t border-slate-100 dark:border-slate-800">
                  {#each runPage.columns as col (col)}
                    <td class="px-3 py-1.5 align-top {BYTE_COLUMNS.has(col) ? 'tabular-nums' : ''}">
                      {cell(row, col)}
                    </td>
                  {/each}
                  <td class="px-3 py-1 align-top text-right">
                    <button
                      class="rounded border border-slate-300 px-2 py-0.5 text-xs text-[var(--accent)] disabled:opacity-40 dark:border-slate-700"
                      onclick={() => row.item_id && (detailId = String(row.item_id))}
                      disabled={!row.item_id}>Open</button>
                  </td>
                </tr>
              {/each}
              {#if runPage.rows.length === 0}
                <tr><td class="px-3 py-3 text-slate-500" colspan={runPage.columns.length + 1}>No rows.</td></tr>
              {/if}
            </tbody>
          </table>
        </div>
        <div class="mt-3 flex items-center gap-3 text-sm">
          <button
            class="rounded-lg border border-slate-300 px-3 py-1 disabled:opacity-40 dark:border-slate-700"
            onclick={() => defs.find((x) => x.id === runId) && run(defs.find((x) => x.id === runId)!, Math.max(0, runOffset - RUN_LIMIT))}
            disabled={runOffset <= 0}>Previous</button>
          <span class="text-slate-500">
            rows {runPage.count === 0 ? 0 : runOffset + 1}–{runOffset + runPage.count}
          </span>
          <button
            class="rounded-lg border border-slate-300 px-3 py-1 disabled:opacity-40 dark:border-slate-700"
            onclick={() => defs.find((x) => x.id === runId) && run(defs.find((x) => x.id === runId)!, runOffset + RUN_LIMIT)}
            disabled={!runPage.has_more}>Next</button>
        </div>
      {/if}
    </div>
  {/if}
</div>

{#if detailId}
  <ItemDetail id={detailId} onClose={() => (detailId = null)} />
{/if}
