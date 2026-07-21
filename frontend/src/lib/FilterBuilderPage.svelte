<script lang="ts">
  import { onMount } from "svelte";
  import ItemDetail from "./ItemDetail.svelte";
  import DslHelp from "./DslHelp.svelte";
  import { copyText } from "./clipboard";
  import { encodeSearchHash } from "./searchparams";
  import { setReportPrefill, takeBuilderPrefill } from "./handoff";
  import {
    previewQuery,
    previewValidationErrors,
    queryKeys,
    search,
    searchTags,
    type QueryPreviewResponse,
    type QueryKeys,
    type ReportValidationError,
  } from "./api";
  import {
    newCondition,
    conditionsToDsl,
    dslToRows,
    conditionsToSearchParams,
    validateCondition,
    opsFor,
    defaultOp,
    type Condition,
    type FieldKind,
    type Op,
  } from "./filterBuilder";

  // ------------------------------------------------------------------ //
  // Visual filter builder (user-requested). Structured condition rows   //
  // compile to the querydsl string (single source of truth); a live     //
  // preview runs it through POST /query/preview (the exact custom-report //
  // machinery) and shows real, RBAC-scoped rows + a capped match count.  //
  // ------------------------------------------------------------------ //

  const FIELDS: { value: FieldKind; label: string }[] = [
    { value: "text", label: "Text" },
    { value: "kind", label: "Kind" },
    { value: "group", label: "Group" },
    { value: "ext", label: "Extension" },
    { value: "size", label: "Size" },
    { value: "modified", label: "Modified" },
    { value: "created", label: "Created" },
    { value: "path", label: "Path" },
    { value: "tag", label: "Tag" },
    { value: "hash", label: "Hash" },
    { value: "meta", label: "Metadata (meta.)" },
    { value: "cf", label: "Custom field (cf.)" },
  ];

  const OP_LABEL: Record<Op, string> = {
    contains: "contains",
    is: "is",
    is_not: "is not",
    "=": "=",
    ">": ">",
    ">=": "≥",
    "<": "<",
    "<=": "≤",
    range: "between",
    matches: "matches",
  };
  const SIZE_UNITS = ["", "K", "M", "G", "T"];
  const DURATION_UNITS = [
    { v: "s", l: "seconds" },
    { v: "m", l: "minutes" },
    { v: "h", l: "hours" },
    { v: "d", l: "days" },
    { v: "w", l: "weeks" },
  ];
  const COMPARATOR_FIELDS = new Set<FieldKind>(["size", "modified", "created", "meta", "cf"]);
  const NEGATABLE = new Set<FieldKind>([
    "text", "kind", "group", "ext", "tag", "size", "modified", "created", "path", "hash", "meta", "cf",
  ]);

  let conditions = $state<Condition[]>([newCondition("kind")]);
  let rawMode = $state(false);
  let rawQuery = $state("");
  let advancedBanner = $state(false);

  // value-picker vocabulary
  let keys = $state<QueryKeys | null>(null);
  let extFacet = $state<string[]>([]);
  let tagSuggest = $state<Record<number, string[]>>({});

  // preview state
  let preview = $state<QueryPreviewResponse | null>(null);
  let vErrors = $state<ReportValidationError[]>([]);
  let previewError = $state("");
  let loading = $state(false);
  let detailId = $state<string | null>(null);

  // search-handoff warning
  let unmappedWarn = $state<string[]>([]);

  const dsl = $derived(rawMode ? rawQuery.trim() : conditionsToDsl(conditions));
  // Distinguish "no rows at all" (matches everything) from "rows present but
  // none complete yet" (mid-typing) so the compiled pane never reads as an error.
  const hasRows = $derived(!rawMode && conditions.length > 0);

  const BYTE_COLUMNS = new Set(["size"]);
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

  // ------------------------------------------------------------------ //
  // Row editing                                                         //
  // ------------------------------------------------------------------ //
  function addRow() {
    conditions = [...conditions, newCondition("text")];
  }
  function removeRow(id: number) {
    conditions = conditions.filter((c) => c.id !== id);
  }
  function onFieldChange(c: Condition) {
    // Reset op + value shape when the field changes so the row stays coherent.
    c.op = defaultOp(c.field);
    c.value = "";
    c.value2 = "";
    c.key = "";
    c.unit = c.field === "size" ? "" : c.field === "modified" || c.field === "created" ? "d" : "";
    c.dateMode = "relative";
    conditions = [...conditions];
  }
  function isNegated(c: Condition): boolean {
    if (c.field === "kind" || c.field === "group" || c.field === "ext" || c.field === "tag") return c.op === "is_not";
    return c.negated;
  }
  function setNegated(c: Condition, val: boolean) {
    if (c.field === "kind" || c.field === "group" || c.field === "ext" || c.field === "tag") c.op = val ? "is_not" : "is";
    else c.negated = val;
    conditions = [...conditions];
  }

  async function onTagInput(c: Condition, q: string) {
    c.value = q;
    conditions = [...conditions];
    try {
      const res = await searchTags(q);
      tagSuggest = { ...tagSuggest, [c.id]: res.tags.map((t) => t.value) };
    } catch {
      /* type-ahead is best-effort */
    }
  }

  // ------------------------------------------------------------------ //
  // Live preview (debounced)                                            //
  // ------------------------------------------------------------------ //
  let debounce: ReturnType<typeof setTimeout> | undefined;
  let previewSeq = 0;
  $effect(() => {
    const current = dsl; // track
    clearTimeout(debounce);
    debounce = setTimeout(() => runPreview(current), 300);
  });

  async function runPreview(query: string) {
    const seq = ++previewSeq;
    loading = true;
    previewError = "";
    vErrors = [];
    try {
      const res = await previewQuery({ query, limit: 25 });
      if (seq !== previewSeq) return;
      preview = res;
    } catch (e) {
      if (seq !== previewSeq) return;
      const errs = previewValidationErrors(e);
      if (errs) {
        vErrors = errs;
        preview = null;
      } else {
        previewError = String(e);
      }
    } finally {
      if (seq === previewSeq) loading = false;
    }
  }

  function errText(e: ReportValidationError): string {
    if (e.error === "parse_error")
      return `Parse error (${e.code}) at position ${e.position}: ${e.reason}`;
    if (e.error === "translation_error") return e.message ?? "cannot translate query";
    return e.message ?? e.error;
  }

  // ------------------------------------------------------------------ //
  // Actions                                                             //
  // ------------------------------------------------------------------ //
  let copied = $state(false);
  async function copyDsl() {
    await copyText(dsl);
    copied = true;
    setTimeout(() => (copied = false), 1200);
  }

  function saveAsReport() {
    setReportPrefill({ query: dsl, columns: ["rel_path", "library", "size"] });
    location.hash = "#/reports";
  }

  function openInSearch() {
    if (rawMode) return;
    const { params, unmapped } = conditionsToSearchParams(conditions);
    unmappedWarn = unmapped
      .map((c) => {
        const line = conditionsToDsl([c]);
        return line || `${c.field} condition`;
      })
      .filter(Boolean);
    location.hash = encodeSearchHash(params);
  }

  function reset() {
    conditions = [newCondition("kind")];
    rawMode = false;
    rawQuery = "";
    advancedBanner = false;
    unmappedWarn = [];
  }

  function toggleRaw() {
    if (!rawMode) {
      // entering raw mode: seed the textarea with the compiled DSL
      rawQuery = conditionsToDsl(conditions);
      rawMode = true;
    } else {
      // leaving raw mode: try to parse the raw DSL back into rows
      const res = dslToRows(rawQuery.trim());
      if (res.advanced) {
        advancedBanner = true;
        return; // keep raw mode; never destroy the query
      }
      conditions = res.rows.length ? res.rows : [newCondition("kind")];
      advancedBanner = false;
      rawMode = false;
    }
  }

  // ------------------------------------------------------------------ //
  // Mount: keys + facets + round-trip prefill                           //
  // ------------------------------------------------------------------ //
  onMount(async () => {
    try {
      keys = await queryKeys();
    } catch {
      /* keys are optional conveniences */
    }
    try {
      const r = await search({ limit: "1" });
      extFacet = Object.entries(r.facets?.extension ?? {})
        .sort((a, b) => b[1] - a[1])
        .slice(0, 40)
        .map(([e]) => e);
    } catch {
      /* facets optional */
    }
    const pf = takeBuilderPrefill();
    if (pf) {
      const res = dslToRows(pf.query);
      if (res.advanced) {
        rawMode = true;
        rawQuery = pf.query;
        advancedBanner = true;
      } else {
        conditions = res.rows.length ? res.rows : [newCondition("kind")];
      }
    }
  });

  const kindOptions = $derived(keys?.kinds ?? []);
  const groupOptions = $derived(keys?.groups ?? []);
  const metaKeyOptions = $derived(keys?.meta_keys.map((k) => k.key) ?? []);
  const cfKeyOptions = $derived(keys?.custom_fields.map((k) => k.name) ?? []);
</script>

<div class="mx-auto max-w-screen-xl">
  <div class="mb-4 flex items-center gap-3">
    <h2 class="text-lg font-semibold">Filter builder</h2>
    <span class="text-sm text-slate-500">Build a query visually, test it live, then save or search.</span>
    <div class="grow"></div>
    <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700" onclick={toggleRaw}>
      {rawMode ? "Back to rows" : "Edit raw DSL"}
    </button>
    <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700" onclick={reset}>Reset</button>
  </div>

  {#if advancedBanner}
    <p class="mb-3 rounded-lg bg-amber-100 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-200">
      This query contains advanced syntax the visual builder can't represent — editing raw. Your query is preserved.
    </p>
  {/if}

  <div class="grid gap-4 lg:grid-cols-2">
    <!-- LEFT: builder -->
    <div>
      {#if rawMode}
        <label class="block text-sm">
          <span class="mb-1 block text-slate-500">Raw filter DSL</span>
          <textarea
            class="h-40 w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-sm dark:border-slate-700"
            bind:value={rawQuery}
            placeholder="kind:video meta.height:>=1080 -tag:archived"></textarea>
        </label>
      {:else}
        <div class="space-y-2">
          {#each conditions as c (c.id)}
            {@const st = validateCondition(c)}
            <div
              class="flex flex-wrap items-center gap-2 rounded-lg border p-2 {st.state === 'invalid'
                ? 'border-red-400 dark:border-red-700'
                : st.state === 'incomplete'
                  ? 'border-amber-400 dark:border-amber-600/70'
                  : 'border-slate-200 dark:border-slate-700'}">
              {#if NEGATABLE.has(c.field)}
                <label class="inline-flex items-center gap-1 text-xs text-slate-500" title="Exclude matches">
                  <input type="checkbox" checked={isNegated(c)} onchange={(e) => setNegated(c, e.currentTarget.checked)} />
                  not
                </label>
              {/if}
              <select
                class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                bind:value={c.field}
                onchange={() => onFieldChange(c)}>
                {#each FIELDS as f (f.value)}
                  <option value={f.value}>{f.label}</option>
                {/each}
              </select>

              {#if (c.field === "meta" || c.field === "cf")}
                <input
                  class="w-36 rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-sm dark:border-slate-700"
                  list={c.field === "meta" ? "meta-keys" : "cf-keys"}
                  placeholder={c.field === "meta" ? "key (e.g. height)" : "field name"}
                  bind:value={c.key} />
              {/if}

              {#if COMPARATOR_FIELDS.has(c.field)}
                <select
                  class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                  bind:value={c.op}>
                  {#each opsFor(c.field) as op (op)}
                    <option value={op}>{OP_LABEL[op]}</option>
                  {/each}
                </select>
              {/if}

              <!-- value inputs, typed per field -->
              {#if c.field === "kind"}
                <select
                  class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                  bind:value={c.value}>
                  <option value="">choose…</option>
                  {#each kindOptions as k (k)}<option value={k}>{k}</option>{/each}
                </select>
              {:else if c.field === "group"}
                <select
                  class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                  bind:value={c.value}>
                  <option value="">choose…</option>
                  {#each groupOptions as g (g)}<option value={g}>{g}</option>{/each}
                </select>
              {:else if c.field === "ext"}
                <input
                  class="w-40 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                  list="ext-facet" placeholder="pdf or mp4;mkv" bind:value={c.value} />
              {:else if c.field === "tag"}
                <input
                  class="w-40 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                  list={`tags-${c.id}`} placeholder="tag" value={c.value}
                  oninput={(e) => onTagInput(c, e.currentTarget.value)} />
                <datalist id={`tags-${c.id}`}>
                  {#each (tagSuggest[c.id] ?? []) as t (t)}<option value={t}></option>{/each}
                </datalist>
              {:else if c.field === "size"}
                <input type="number" min="0"
                  class="w-24 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                  bind:value={c.value} />
                {#if c.op === "range"}
                  <span class="text-xs text-slate-500">to</span>
                  <input type="number" min="0"
                    class="w-24 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                    bind:value={c.value2} />
                {/if}
                <select
                  class="rounded-lg border border-slate-300 bg-transparent px-1 py-1 text-sm dark:border-slate-700"
                  bind:value={c.unit}>
                  {#each SIZE_UNITS as u (u)}<option value={u}>{u === "" ? "B" : u + "iB"}</option>{/each}
                </select>
              {:else if c.field === "modified" || c.field === "created"}
                <select
                  class="rounded-lg border border-slate-300 bg-transparent px-1 py-1 text-sm dark:border-slate-700"
                  bind:value={c.dateMode}>
                  <option value="relative">relative</option>
                  <option value="absolute">date</option>
                </select>
                {#if c.dateMode === "relative"}
                  <input type="number" min="0"
                    class="w-20 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                    bind:value={c.value} />
                  {#if c.op === "range"}
                    <span class="text-xs text-slate-500">to</span>
                    <input type="number" min="0"
                      class="w-20 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                      bind:value={c.value2} />
                  {/if}
                  <select
                    class="rounded-lg border border-slate-300 bg-transparent px-1 py-1 text-sm dark:border-slate-700"
                    bind:value={c.unit}>
                    {#each DURATION_UNITS as u (u.v)}<option value={u.v}>{u.l} ago</option>{/each}
                  </select>
                {:else}
                  <input type="date"
                    class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                    bind:value={c.value} />
                  {#if c.op === "range"}
                    <span class="text-xs text-slate-500">to</span>
                    <input type="date"
                      class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                      bind:value={c.value2} />
                  {/if}
                {/if}
              {:else if c.field === "meta" || c.field === "cf"}
                <input
                  class="w-40 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                  placeholder="value" bind:value={c.value} />
                {#if c.op === "range"}
                  <span class="text-xs text-slate-500">to</span>
                  <input
                    class="w-40 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                    placeholder="value" bind:value={c.value2} />
                {/if}
              {:else}
                <!-- text / path / hash -->
                <input
                  class="w-56 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                  placeholder={c.field === "path" ? "*/backups/*" : c.field === "hash" ? "hex digest" : "text"}
                  bind:value={c.value} />
              {/if}

              <div class="grow"></div>
              <button
                class="rounded border border-slate-300 px-2 py-0.5 text-xs text-red-600 dark:border-slate-700"
                onclick={() => removeRow(c.id)}
                aria-label="Remove condition">✕</button>
              {#if st.state !== "ok"}
                <p
                  class="w-full text-xs {st.state === 'invalid'
                    ? 'text-red-600 dark:text-red-400'
                    : 'text-amber-600 dark:text-amber-500'}">
                  {st.state === "invalid" ? "⚠ " : ""}{st.hint}{st.state === "invalid"
                    ? " — excluded until fixed"
                    : ""}
                </p>
              {/if}
            </div>
          {/each}
          {#if conditions.length === 0}
            <p class="text-sm text-slate-500">No conditions — matches all items.</p>
          {/if}
        </div>

        <button
          class="mt-3 rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
          onclick={addRow}>+ Add condition</button>
        <p class="mt-1 text-xs text-slate-400">Conditions are combined with AND. (OR groups aren't in the query grammar yet.)</p>
        <DslHelp onInsert={(frag) => { rawQuery = (conditionsToDsl(conditions) + " " + frag).trim(); rawMode = true; }} context="search" />
      {/if}

      <!-- compiled DSL preview -->
      <div class="mt-4">
        <div class="mb-1 flex items-center gap-2">
          <span class="text-xs text-slate-500">Compiled filter DSL</span>
          <div class="grow"></div>
          <button class="rounded border border-slate-300 px-2 py-0.5 text-xs dark:border-slate-700" onclick={copyDsl}>
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <pre class="overflow-x-auto rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 font-mono text-xs dark:border-slate-800 dark:bg-slate-900">{dsl || (hasRows ? "(no complete conditions yet)" : "(all items)")}</pre>
      </div>

      <div class="mt-3 flex flex-wrap gap-2">
        <button class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white" onclick={saveAsReport}>Save as custom report</button>
        <button
          class="rounded-lg border border-slate-300 px-3 py-1 text-sm disabled:opacity-40 dark:border-slate-700"
          onclick={openInSearch} disabled={rawMode} title={rawMode ? "Switch back to rows to map into search" : ""}>Open in search</button>
      </div>
      {#if unmappedWarn.length}
        <div class="mt-2 rounded-lg bg-amber-100 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-200">
          Some conditions have no equivalent search filter and were dropped when opening search:
          <ul class="mt-1 list-disc pl-5 font-mono">
            {#each unmappedWarn as w (w)}<li>{w}</li>{/each}
          </ul>
        </div>
      {/if}
    </div>

    <!-- RIGHT: live results -->
    <div>
      <div class="mb-2 flex items-center gap-2">
        <span class="text-sm font-medium">Live results</span>
        {#if preview}
          <span class="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            {preview.total}{preview.total_capped ? "+" : ""} match{preview.total === 1 && !preview.total_capped ? "" : "es"}
          </span>
        {/if}
        {#if loading}<span class="text-xs text-slate-400">testing…</span>{/if}
      </div>

      {#if vErrors.length}
        <ul class="space-y-1">
          {#each vErrors as e, i (i)}
            <li class="rounded bg-amber-100 px-2 py-1 text-xs text-amber-900 dark:bg-amber-950 dark:text-amber-200">{errText(e)}</li>
          {/each}
        </ul>
      {:else if previewError}
        <p class="rounded bg-red-100 px-2 py-1 text-sm text-red-800 dark:bg-red-950 dark:text-red-200">{previewError}</p>
      {:else if preview}
        <div class="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
          <table class="w-full text-sm">
            <thead class="bg-slate-50 text-left dark:bg-slate-900">
              <tr>
                <th class="px-2 py-1.5 font-medium">filename</th>
                <th class="px-2 py-1.5 font-medium">library</th>
                <th class="px-2 py-1.5 font-medium">type</th>
                <th class="px-2 py-1.5 font-medium">size</th>
                <th class="px-2 py-1.5 font-medium"></th>
              </tr>
            </thead>
            <tbody>
              {#each preview.rows as row, i (i)}
                <tr class="border-t border-slate-100 dark:border-slate-800">
                  <td class="px-2 py-1 align-top" title={String(row.rel_path ?? "")}>{cell(row, "filename")}</td>
                  <td class="px-2 py-1 align-top">{cell(row, "library")}</td>
                  <td class="px-2 py-1 align-top">{cell(row, "file_category")}</td>
                  <td class="px-2 py-1 align-top tabular-nums">{cell(row, "size")}</td>
                  <td class="px-2 py-1 align-top text-right">
                    <button
                      class="rounded border border-slate-300 px-2 py-0.5 text-xs text-[var(--accent)] disabled:opacity-40 dark:border-slate-700"
                      onclick={() => row.item_id && (detailId = String(row.item_id))}
                      disabled={!row.item_id}>Open</button>
                  </td>
                </tr>
              {/each}
              {#if preview.rows.length === 0}
                <tr><td class="px-2 py-3 text-slate-500" colspan="5">No matches.</td></tr>
              {/if}
            </tbody>
          </table>
        </div>
        {#if preview.has_more}
          <p class="mt-2 text-xs text-slate-400">Showing the first {preview.count}. Save as a custom report to export the full result.</p>
        {/if}
      {:else}
        <p class="text-sm text-slate-500">Add a condition to see matching items.</p>
      {/if}
    </div>
  </div>

  <!-- shared datalists for meta/cf/ext value pickers -->
  <datalist id="meta-keys">{#each metaKeyOptions as k (k)}<option value={k}></option>{/each}</datalist>
  <datalist id="cf-keys">{#each cfKeyOptions as k (k)}<option value={k}></option>{/each}</datalist>
  <datalist id="ext-facet">{#each extFacet as e (e)}<option value={e}></option>{/each}</datalist>
</div>

{#if detailId}
  <ItemDetail id={detailId} onClose={() => (detailId = null)} />
{/if}
