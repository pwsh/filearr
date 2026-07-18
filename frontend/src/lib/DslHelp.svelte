<script lang="ts">
  // FIX-12 (Item B) — expandable "Query syntax" help, shared by the Search page
  // and the Custom-reports builder. Content comes from ./dslHelp (one source of
  // truth, backend-verified against the real parser). Chips INSERT their example
  // into the caller's query box via `onInsert`. Collapsed by default; the chevron
  // toggle matches the Filters/Saved panels' existing style.
  import { DSL_SECTIONS } from "./dslHelp";

  let {
    onInsert,
    context = "search",
  }: { onInsert: (frag: string) => void; context?: "search" | "reports" } = $props();

  let open = $state(false);
</script>

<div class="mt-2">
  <button
    type="button"
    class="flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
    aria-expanded={open}
    onclick={() => (open = !open)}
  >
    <span>Query syntax</span>
    <span aria-hidden="true">{open ? "▲" : "▾"}</span>
  </button>

  {#if open}
    <div class="mt-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
      <p class="text-xs text-slate-500">
        Combine free text with <code class="font-mono">key:value</code> filters —
        everything is ANDed. Click an example to add it to your query.
      </p>
      <div class="mt-3 grid gap-3 sm:grid-cols-2">
        {#each DSL_SECTIONS as sec (sec.title)}
          {@const blocked = context === "reports" && !!sec.searchOnly}
          <div>
            <div class="flex flex-wrap items-center gap-2">
              <span class="font-mono text-sm font-medium">{sec.title}</span>
              {#if sec.searchOnly}
                <span
                  class="rounded-full bg-slate-200 px-2 py-0.5 text-[10px] text-slate-600 dark:bg-slate-700 dark:text-slate-300"
                >{context === "reports" ? "not supported in reports" : "search only"}</span>
              {/if}
            </div>
            <p class="mt-0.5 text-xs text-slate-500">{sec.body}</p>
            <div class="mt-1 flex flex-wrap gap-1.5">
              {#each sec.examples as ex (ex.q)}
                <button
                  type="button"
                  class="rounded-full border border-slate-300 px-2 py-0.5 font-mono text-xs hover:border-[var(--accent)] hover:text-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700"
                  title={blocked
                    ? "Fuzzy terms are not supported in reports"
                    : (ex.note ?? `Insert ${ex.q}`)}
                  disabled={blocked}
                  onclick={() => onInsert(ex.q)}
                >{ex.q}</button>
              {/each}
            </div>
          </div>
        {/each}
      </div>
    </div>
  {/if}
</div>
