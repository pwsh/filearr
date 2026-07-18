<script lang="ts">
  // A compact row of "big fact" stat chips (label + value). Values are text-only
  // and any null/empty fact is dropped, so a card can pass its full candidate set
  // and only the present ones render.
  let { facts }: { facts: { label: string; value: string | null }[] } = $props();
  const shown = $derived(facts.filter((f) => f.value != null && f.value !== ""));
</script>

{#if shown.length}
  <div class="flex flex-wrap gap-3">
    {#each shown as f (f.label)}
      <div class="rounded-lg bg-slate-100 px-3 py-2 dark:bg-slate-800">
        <div class="text-[10px] uppercase tracking-wide text-slate-500">{f.label}</div>
        <div class="font-medium">{f.value}</div>
      </div>
    {/each}
  </div>
{/if}
