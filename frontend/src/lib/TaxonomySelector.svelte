<script lang="ts">
  // W8 — library type-gating selector over the file taxonomy. Replaces the old
  // flat media-type chip row. Two bound arrays go to the backend:
  //   enabled_categories — a category key here includes ALL of its groups
  //   enabled_groups     — individually-picked group keys (when the whole
  //                        category is NOT selected)
  // Empty both = "all file types". Selecting a category visually implies (and
  // disables) its groups; expand a category to drill in and pick single groups.
  import type { TaxonomyNode } from "./api";

  let {
    tree,
    categories = $bindable(),
    groups = $bindable(),
  }: {
    tree: TaxonomyNode[];
    categories: string[];
    groups: string[];
  } = $props();

  // Which categories are expanded to show their individual groups.
  let open = $state<Record<string, boolean>>({});

  const catSelected = (key: string): boolean => categories.includes(key);

  function toggleCategory(node: TaxonomyNode) {
    const key = node.category.key;
    if (catSelected(key)) {
      categories = categories.filter((c) => c !== key);
    } else {
      categories = [...categories, key];
      // Whole-category selection supersedes any individual group picks for it —
      // drop them so the payload isn't redundant.
      const groupKeys = new Set(node.groups.map((g) => g.key));
      groups = groups.filter((g) => !groupKeys.has(g));
    }
  }

  function toggleGroup(key: string) {
    groups = groups.includes(key) ? groups.filter((g) => g !== key) : [...groups, key];
  }

  // How many groups of a category are individually selected (for the collapsed hint).
  function pickedIn(node: TaxonomyNode): number {
    return node.groups.filter((g) => groups.includes(g.key)).length;
  }

  const nothingSelected = $derived(categories.length === 0 && groups.length === 0);
</script>

<div class="flex flex-col gap-2">
  {#if tree.length === 0}
    <p class="text-xs text-slate-400">
      No taxonomy available (the catalog is empty or the taxonomy service is offline).
      Leaving this empty includes all file types.
    </p>
  {:else}
    {#each tree as node (node.category.key)}
      {@const cat = node.category}
      {@const on = catSelected(cat.key)}
      {@const picked = pickedIn(node)}
      <div class="rounded-lg border border-slate-200 dark:border-slate-800">
        <div class="flex items-center gap-2 px-2 py-1.5">
          <label class="inline-flex items-center gap-2 text-sm" title={cat.description}>
            <input type="checkbox" checked={on} onchange={() => toggleCategory(node)} />
            <span class="font-medium">{cat.label}</span>
          </label>
          {#if on}
            <span class="rounded bg-[var(--accent)]/15 px-1.5 py-0.5 text-[10px] text-[var(--accent)]">all groups</span>
          {:else if picked > 0}
            <span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500 dark:bg-slate-800">{picked} group{picked === 1 ? "" : "s"}</span>
          {/if}
          <span class="grow"></span>
          {#if node.groups.length}
            <button
              type="button"
              class="text-xs text-[var(--accent)]"
              onclick={() => (open = { ...open, [cat.key]: !open[cat.key] })}>
              {open[cat.key] ? "hide groups" : `groups (${node.groups.length})`}
            </button>
          {/if}
        </div>
        {#if open[cat.key] && node.groups.length}
          <div class="flex flex-wrap gap-2 border-t border-slate-100 px-3 py-2 dark:border-slate-800/70">
            {#each node.groups as g (g.key)}
              <label
                class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs {on
                  ? 'border-transparent bg-[var(--accent)]/10 text-[var(--accent)]'
                  : groups.includes(g.key)
                    ? 'border-transparent bg-[var(--accent)] text-white'
                    : 'border-slate-300 dark:border-slate-700'}"
                title={on ? "Included via the whole category" : g.description || g.label}>
                <input
                  type="checkbox"
                  class="h-3 w-3"
                  disabled={on}
                  checked={on || groups.includes(g.key)}
                  onchange={() => toggleGroup(g.key)} />
                {g.label}
              </label>
            {/each}
          </div>
        {/if}
      </div>
    {/each}
    <p class="text-xs {nothingSelected ? 'text-slate-500' : 'text-slate-400'}">
      {nothingSelected
        ? "Nothing selected — all file types are included."
        : "Only the selected categories/groups will be scanned."}
    </p>
  {/if}
</div>
