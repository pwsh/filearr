<script lang="ts">
  // UI-T5 — a small "?" help affordance placed beside a labelled field. Clicking
  // toggles a plain-text popover; the raw text is ALSO exposed via the `title`
  // attribute so hover + assistive tech get it even without opening the popover.
  // Text is rendered as text (interpolation, never {@html}) — help copy is static
  // and trusted, but we keep the no-@html discipline uniform across the app.
  let { text, label = "field" }: { text: string; label?: string } = $props();

  let open = $state(false);
  let root = $state<HTMLElement | null>(null);

  // Close on any click outside the popover, and on Escape.
  function onWindowClick(e: MouseEvent) {
    if (open && root && !root.contains(e.target as Node)) open = false;
  }
  function onKey(e: KeyboardEvent) {
    if (open && e.key === "Escape") open = false;
  }
</script>

<svelte:window onclick={onWindowClick} onkeydown={onKey} />

<span class="relative inline-block align-middle" bind:this={root}>
  <button
    type="button"
    class="inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-[10px]
           leading-none text-slate-500 hover:border-[var(--accent)] hover:text-[var(--accent)]
           dark:border-slate-600 dark:text-slate-400"
    aria-label={`Help: ${label}`}
    aria-expanded={open}
    title={text}
    onclick={(e) => {
      e.stopPropagation();
      open = !open;
    }}>?</button>

  {#if open}
    <span
      role="tooltip"
      class="absolute left-0 top-5 z-50 block w-64 rounded-lg border border-slate-200 bg-white p-2 text-xs
             font-normal leading-snug text-slate-600 shadow-lg dark:border-slate-700 dark:bg-slate-800
             dark:text-slate-300">{text}</span>
  {/if}
</span>
