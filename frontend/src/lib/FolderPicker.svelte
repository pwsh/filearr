<script lang="ts">
  // UI-T4 (frontend) — server-side folder browser dialog. Lists directories only,
  // rooted at the backend's allowlist (FILEARR_BROWSE_ROOTS). The user navigates
  // via breadcrumb / parent / dir list and confirms a selection that feeds
  // root_path. Free-text entry stays available on the parent form; this is an
  // optional aid. A 422 (outside allowlist / traversal) surfaces inline rather
  // than blowing up the dialog.
  import { browseFs, type FsBrowse } from "./api";

  let {
    initial = "",
    onPick,
    onClose,
  }: { initial?: string; onPick: (path: string) => void; onClose: () => void } = $props();

  let view = $state<FsBrowse | null>(null);
  let error = $state("");
  let loading = $state(false);

  async function go(path: string) {
    loading = true;
    error = "";
    try {
      view = await browseFs(path);
    } catch (e) {
      // Keep the previous listing visible so the user can recover.
      error = String(e);
    } finally {
      loading = false;
    }
  }

  // Breadcrumb: split the current absolute path into cumulative segments so each
  // ancestor is individually clickable. Derived from the server-reported `path`
  // (never from local string math on user input).
  function crumbs(path: string): { label: string; path: string }[] {
    if (!path) return [];
    const parts = path.split("/").filter(Boolean);
    const out: { label: string; path: string }[] = [];
    let acc = "";
    for (const p of parts) {
      acc = `${acc}/${p}`;
      out.push({ label: p, path: acc });
    }
    return out;
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") onClose();
  }

  $effect(() => {
    void go(initial);
  });
</script>

<svelte:window onkeydown={onKey} />

<div class="fixed inset-0 z-[60] overflow-y-auto">
  <button
    type="button"
    class="absolute inset-0 h-full w-full cursor-default bg-black/50"
    aria-label="Close folder picker"
    onclick={onClose}
  ></button>

  <div
    class="relative z-10 mx-auto mt-24 mb-8 w-full max-w-lg rounded-2xl bg-white p-5 shadow-xl dark:bg-slate-900"
    role="dialog"
    aria-modal="true"
    aria-label="Choose a folder"
  >
    <div class="flex items-center gap-2 border-b border-slate-200 pb-3 dark:border-slate-800">
      <h3 class="text-sm font-semibold">Choose a folder</h3>
      <span class="grow"></span>
      <button
        class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
        onclick={onClose}>Close</button>
    </div>

    <!-- Breadcrumb: roots shortcut + each ancestor segment. -->
    <div class="mt-3 flex flex-wrap items-center gap-1 text-xs text-slate-500">
      <button class="rounded px-1 hover:text-[var(--accent)] hover:underline" onclick={() => go("")}>
        roots
      </button>
      {#if view?.path}
        {#each crumbs(view.path) as c (c.path)}
          <span>/</span>
          <button class="rounded px-1 hover:text-[var(--accent)] hover:underline" onclick={() => go(c.path)}>
            {c.label}
          </button>
        {/each}
      {/if}
    </div>

    {#if error}
      <p class="mt-3 text-xs text-red-500">{error}</p>
    {/if}

    <div class="mt-3 max-h-72 overflow-y-auto rounded-lg border border-slate-200 dark:border-slate-800">
      {#if loading && !view}
        <p class="p-3 text-sm text-slate-500">Loading…</p>
      {:else if view}
        <ul class="divide-y divide-slate-100 text-sm dark:divide-slate-800">
          {#if view.parent !== null}
            <li>
              <button
                class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-800/50"
                onclick={() => go(view!.parent ?? "")}>
                <span class="text-slate-400">↑</span> ..
              </button>
            </li>
          {/if}
          {#if !view.path && view.roots.length}
            {#each view.roots as r (r)}
              <li>
                <button
                  class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-800/50"
                  onclick={() => go(r)}>
                  <span class="text-slate-400">▸</span>
                  <span class="font-mono">{r}</span>
                </button>
              </li>
            {/each}
          {:else if view.dirs.length}
            {#each view.dirs as d (d.path)}
              <li>
                <button
                  class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-slate-50 dark:hover:bg-slate-800/50"
                  onclick={() => go(d.path)}>
                  <span class="text-slate-400">▸</span> {d.name}
                </button>
              </li>
            {/each}
          {:else}
            <li class="px-3 py-2 text-slate-500">No sub-folders here.</li>
          {/if}
        </ul>
      {/if}
    </div>

    <div class="mt-4 flex items-center gap-2">
      <span class="grow truncate font-mono text-xs text-slate-500">{view?.path || "(no folder selected)"}</span>
      <button
        class="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm text-white disabled:opacity-50"
        disabled={!view?.path}
        onclick={() => view?.path && onPick(view.path)}>
        Use this folder
      </button>
    </div>
  </div>
</div>
