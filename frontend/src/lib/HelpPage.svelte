<script lang="ts">
  // FIX-8 (UI docs links): the standalone Help page (#/help). Renders the same
  // field-help copy the inline "?" popovers use (lib/help.ts), grouped by topic,
  // so a user can read every setting's explanation in one place. A simple
  // client-side filter narrows the list. Any HELP key not assigned to a topic is
  // collected under "Other" so a new HELP entry is never silently dropped.
  import { HELP, HELP_TOPICS, type HelpTopic } from "./help";

  let query = $state("");

  // Keys already placed in a topic; the remainder land under a synthetic "Other".
  const grouped = $derived.by(() => {
    const placed = new Set<string>();
    for (const t of HELP_TOPICS) for (const [k] of t.items) placed.add(k);
    const other: [string, string][] = Object.keys(HELP)
      .filter((k) => !placed.has(k))
      .map((k) => [k, k] as [string, string]);
    const topics: HelpTopic[] = [...HELP_TOPICS];
    if (other.length) topics.push({ title: "Other", items: other });
    return topics;
  });

  function matches(label: string, key: string, text: string): boolean {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return (
      label.toLowerCase().includes(q) ||
      key.toLowerCase().includes(q) ||
      text.toLowerCase().includes(q)
    );
  }

  // Topics with at least one matching item under the current filter.
  const visible = $derived(
    grouped
      .map((t) => ({
        title: t.title,
        items: t.items.filter(([k, label]) => matches(label, k, HELP[k] ?? "")),
      }))
      .filter((t) => t.items.length > 0),
  );
</script>

<section class="mx-auto max-w-3xl">
  <h2 class="mb-1 text-xl font-semibold">Help &amp; field reference</h2>
  <p class="mb-4 text-sm text-slate-500 dark:text-slate-400">
    Explanations for every configurable setting, grouped by topic. The same text
    appears behind the small “?” buttons next to each field.
  </p>

  <input
    type="search"
    bind:value={query}
    placeholder="Filter help…"
    aria-label="Filter help topics"
    class="mb-6 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm
           dark:border-slate-700 dark:bg-slate-900"
  />

  {#if visible.length === 0}
    <p class="text-sm text-slate-500 dark:text-slate-400">No help entries match “{query}”.</p>
  {/if}

  {#each visible as topic (topic.title)}
    <div class="mb-8">
      <h3 class="mb-3 border-b border-slate-200 pb-1 text-sm font-semibold uppercase
                 tracking-wide text-[var(--accent)] dark:border-slate-800">
        {topic.title}
      </h3>
      <dl class="space-y-4">
        {#each topic.items as [key, label] (key)}
          <div>
            <dt class="text-sm font-medium text-slate-800 dark:text-slate-200">{label}</dt>
            <dd class="mt-0.5 text-sm leading-snug text-slate-600 dark:text-slate-400">
              {HELP[key]}
            </dd>
          </div>
        {/each}
      </dl>
    </div>
  {/each}
</section>
