<script lang="ts">
  import { asStr } from "./format";

  // Render an array of homogeneous track/chapter objects as a small table. The
  // column set is the union of the rows' keys (stable first-seen order). Every
  // cell is rendered as text (asStr), never markup.
  let {
    title,
    rows,
  }: { title: string; rows: Record<string, unknown>[] } = $props();

  const columns = $derived.by(() => {
    const cols: string[] = [];
    for (const r of rows) for (const k of Object.keys(r)) if (!cols.includes(k)) cols.push(k);
    return cols;
  });
</script>

{#if rows.length && columns.length}
  <div>
    <div class="mb-1 text-xs font-semibold text-slate-500">{title} ({rows.length})</div>
    <div class="overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
      <table class="w-full text-left text-xs">
        <thead class="bg-slate-50 text-slate-500 dark:bg-slate-800/50">
          <tr>
            {#each columns as c (c)}<th class="px-2 py-1 font-medium">{c}</th>{/each}
          </tr>
        </thead>
        <tbody>
          {#each rows as r, i (i)}
            <tr class="border-t border-slate-100 dark:border-slate-800">
              {#each columns as c (c)}<td class="px-2 py-1 tabular-nums">{asStr(r[c]) ?? ""}</td>{/each}
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>
{/if}
