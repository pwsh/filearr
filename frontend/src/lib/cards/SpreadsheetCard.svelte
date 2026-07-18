<script lang="ts">
  import Highlights from "./Highlights.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { asStr } from "./format";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  const CURATED = [
    "sheets", "sheet_count", "author", "subject", "created", "modified",
    "title", "unsupported",
  ];

  const highlights = $derived([
    { label: "Sheets", value: asStr(m.sheet_count) },
    { label: "Author", value: asStr(m.author) },
    { label: "Created", value: asStr(m.created) },
    { label: "Modified", value: asStr(m.modified) },
  ]);
  const sheets = $derived(Array.isArray(m.sheets) ? (m.sheets as unknown[]).map(String) : []);
</script>

<div class="space-y-4">
  <Highlights facts={highlights} />
  {#if sheets.length}
    <div>
      <div class="mb-1 text-xs font-semibold text-slate-500">Sheet names</div>
      <div class="flex flex-wrap gap-1">
        {#each sheets as s, i (i)}
          <span class="rounded bg-slate-100 px-2 py-0.5 text-xs dark:bg-slate-800">{s}</span>
        {/each}
      </div>
    </div>
  {/if}
  <KeyFactsCard {item} exclude={CURATED} heading="More details" />
</div>
