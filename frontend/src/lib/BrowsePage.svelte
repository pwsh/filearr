<script lang="ts">
  import { copyText } from "./clipboard";
  // UI-T12 Part 3 — in-page folder navigation. Renders the breadcrumb header
  // (shared Breadcrumbs component), a folder list (click → deeper), and the file
  // list (click → ItemDetail modal). Back/forward work because navigation is
  // pure hash mutation (App owns the router; this component reads props and emits
  // hash changes via gotoBrowse). All names render as text (untrusted).
  import { libraryTree, listLibraries, type Library, type TreeResponse } from "./api";
  import { buildDisplayPath, buildOpenUrl } from "./pathlinks";
  import { formatShare, shareLocation } from "./osFormat";
  import { shareFormat, detectedPlatform } from "./osFormat.svelte";
  import { gotoBrowse } from "./routes";
  import Breadcrumbs from "./Breadcrumbs.svelte";
  import ItemDetail from "./ItemDetail.svelte";
  import Thumb from "./Thumb.svelte";

  let { libraryId, path }: { libraryId: string; path: string } = $props();

  const LIMIT = 100;

  let tree = $state<TreeResponse | null>(null);
  let error = $state("");
  let selected = $state<string | null>(null);
  let offset = $state(0);
  // UI-T15: raw url-ish + UNC spellings from the library; the effective one
  // (per the viewer's OS / override) is derived below and drives every affordance.
  let shareUrlRaw = $state<string | null>(null);
  let shareUncRaw = $state<string | null>(null);
  const sharePrefix = $derived(
    formatShare(shareLocation(shareUrlRaw, shareUncRaw), shareFormat.pref, detectedPlatform),
  );
  let libName = $state<string>("");
  let copied = $state<string | null>(null);
  let copyTimer: ReturnType<typeof setTimeout>;
  let openFolderMenu = $state<string | null>(null);
  // P12 slice 2: list vs. thumbnail-grid layout for the Files section. Ephemeral
  // (component-local) -- a cheap presentational toggle, no deep-link needed here.
  let filesView = $state<"list" | "grid">("list");

  // Resolve the library's EFFECTIVE share prefix + name once (the tree endpoint
  // returns the name but not the share prefix). OPS-T7: share_prefix_effective
  // folds in the deploy mount map, so open/copy work even with no manual prefix.
  // Refreshed if the library id changes.
  let resolvedFor = "";
  async function resolveLibrary(id: string) {
    if (resolvedFor === id) return;
    try {
      const libs: Library[] = await listLibraries();
      const lib = libs.find((l) => l.id === id);
      shareUrlRaw = lib?.share_prefix_effective ?? lib?.share_prefix ?? null;
      shareUncRaw = lib?.share_unc_effective ?? null;
      libName = lib?.name ?? "";
      resolvedFor = id;
    } catch {
      /* name falls back to the tree payload below */
    }
  }

  async function load() {
    error = "";
    try {
      await resolveLibrary(libraryId);
      const res = await libraryTree(libraryId, path, LIMIT, offset);
      // Folders are server-paginated (cap 500/page). Auto-fetch the remaining
      // pages so huge directories (e.g. >500 shows) always render completely —
      // folder rows are tiny, so this stays cheap even at a few thousand.
      let guard = 0;
      while (res.folders.length < res.folders_total && guard < 20) {
        const more = await libraryTree(
          libraryId, path, LIMIT, offset, res.folders.length,
        );
        if (more.folders.length === 0) break;
        res.folders = [...res.folders, ...more.folders];
        guard += 1;
      }
      tree = res;
      if (!libName) libName = res.library_name;
    } catch (e) {
      tree = null;
      error = String(e);
    }
  }

  // Reset pagination whenever the target folder changes, then (re)load. Reading
  // offset inside the effect makes it re-run on Prev/Next too.
  let lastKey = "";
  $effect(() => {
    const key = `${libraryId} ${path}`;
    if (key !== lastKey) {
      lastKey = key;
      offset = 0;
    }
    void offset;
    load();
  });

  async function copy(label: string, textToCopy: string) {
    try {
      await copyText(textToCopy);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = textToCopy;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch {
        /* ignore */
      }
      document.body.removeChild(ta);
    }
    copied = label;
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => (copied = null), 1200);
    openFolderMenu = null;
  }

  function folderRel(name: string): string {
    return path ? `${path}/${name}` : name;
  }

  const shownFrom = $derived(tree && tree.total_items > 0 ? offset + 1 : 0);
  const shownTo = $derived(tree ? Math.min(offset + LIMIT, tree.total_items) : 0);

  function fmtSize(n: number): string {
    if (n < 1024) return `${n} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let v = n / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return `${v.toFixed(1)} ${units[i]}`;
  }
</script>

<div class="mx-auto max-w-5xl">
  <div class="mt-2 rounded-xl border border-slate-200 p-3 dark:border-slate-800">
    <Breadcrumbs
      libraryName={libName || tree?.library_name || "Library"}
      shareUrl={shareUrlRaw}
      shareUnc={shareUncRaw}
      relPath={path}
      isFile={false}
      onNavigate={(p) => gotoBrowse(libraryId, p)}
    />
  </div>

  {#if error}
    <p class="mt-6 text-red-500">{error}</p>
  {:else if !tree}
    <p class="mt-6 text-slate-500">Loading…</p>
  {:else}
    {#if tree.folders.length === 0 && tree.items.length === 0}
      <p class="mt-6 text-slate-500">This folder is empty.</p>
    {/if}

    {#if tree.folders.length > 0}
      <h3 class="mt-6 text-sm font-semibold text-slate-500">Folders</h3>
      <ul class="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {#each tree.folders as f (f.name)}
          <li class="relative flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 dark:border-slate-800">
            <button
              type="button"
              class="flex min-w-0 grow items-center gap-2 text-left"
              onclick={() => gotoBrowse(libraryId, folderRel(f.name))}>
              <span class="text-slate-400">📁</span>
              <span class="truncate font-medium" title={f.name}>{f.name}</span>
              <span class="ml-auto shrink-0 text-xs text-slate-400">{f.item_count}</span>
            </button>
            <button
              type="button"
              class="rounded px-1 text-xs text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800"
              aria-label="Folder actions for {f.name}"
              title="Folder actions"
              onclick={() => (openFolderMenu = openFolderMenu === f.name ? null : f.name)}>⋯</button>
            {#if openFolderMenu === f.name}
              <div
                class="absolute right-2 top-full z-20 mt-1 w-max min-w-[10rem] rounded-lg border border-slate-200 bg-white p-1 text-xs shadow-lg dark:border-slate-700 dark:bg-slate-800"
                role="menu">
                {#if sharePrefix}
                  <a
                    class="block rounded px-2 py-1 hover:bg-slate-100 dark:hover:bg-slate-700"
                    href={buildOpenUrl(sharePrefix, folderRel(f.name))}
                    target="_blank"
                    rel="noopener noreferrer"
                    title="Open the folder at its network location. Browsers may block file:// — use Copy path if nothing happens."
                    onclick={() => (openFolderMenu = null)}>Open via network</a>
                {/if}
                <button
                  type="button"
                  class="block w-full rounded px-2 py-1 text-left hover:bg-slate-100 dark:hover:bg-slate-700"
                  onclick={() =>
                    copy(
                      `folder-${f.name}`,
                      sharePrefix ? buildDisplayPath(sharePrefix, folderRel(f.name)) : folderRel(f.name),
                    )}>
                  {copied === `folder-${f.name}` ? "Copied!" : "Copy path"}
                </button>
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}

    {#if tree.items.length > 0}
      <div class="mt-6 flex items-baseline gap-3">
        <h3 class="text-sm font-semibold text-slate-500">Files</h3>
        <span class="text-xs text-slate-400">showing {shownFrom}–{shownTo} of {tree.total_items}</span>
        <span class="grow"></span>
        <div class="flex items-center overflow-hidden rounded-full border border-slate-300 text-xs dark:border-slate-700" role="group" aria-label="File layout">
          <button
            type="button"
            class="px-2 py-0.5 {filesView === 'list' ? 'bg-[var(--accent)] text-white' : ''}"
            aria-pressed={filesView === 'list'}
            onclick={() => (filesView = 'list')}>List</button>
          <button
            type="button"
            class="px-2 py-0.5 {filesView === 'grid' ? 'bg-[var(--accent)] text-white' : ''}"
            aria-pressed={filesView === 'grid'}
            onclick={() => (filesView = 'grid')}>Grid</button>
        </div>
      </div>
      {#if filesView === "grid"}
        <div class="mt-2 grid gap-3 grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
          {#each tree.items as it (it.id)}
            <button
              type="button"
              class="flex flex-col overflow-hidden rounded-lg border border-slate-200 text-left hover:border-slate-300 dark:border-slate-800 dark:hover:border-slate-700"
              onclick={() => (selected = it.id)}>
              <Thumb id={it.id} size="aspect-square w-full h-auto" rounded="rounded-none" />
              <div class="flex flex-col gap-1 p-2">
                <span class="truncate text-sm font-medium" title={it.filename}>{it.title ?? it.filename}</span>
                <span class="flex items-center gap-1">
                  <span class="shrink-0 rounded bg-slate-200 px-1.5 py-0.5 text-[10px] dark:bg-slate-800">{it.media_type}</span>
                  <span class="ml-auto shrink-0 text-[10px] text-slate-400">{fmtSize(it.size)}</span>
                </span>
              </div>
            </button>
          {/each}
        </div>
      {:else}
      <ul class="mt-2 divide-y divide-slate-200 dark:divide-slate-800">
        {#each tree.items as it (it.id)}
          <li>
            <button
              type="button"
              class="flex w-full items-center gap-3 py-3 text-left hover:bg-slate-50 dark:hover:bg-slate-800/50"
              onclick={() => (selected = it.id)}>
              <Thumb id={it.id} size="h-9 w-9" />
              <span class="rounded bg-slate-200 px-2 py-0.5 text-xs dark:bg-slate-800">{it.media_type}</span>
              <span class="truncate font-medium" title={it.filename}>{it.title ?? it.filename}</span>
              {#if it.year}<span class="text-sm text-slate-500">({it.year})</span>{/if}
              <span class="grow"></span>
              <span class="shrink-0 text-xs text-slate-400">{fmtSize(it.size)}</span>
            </button>
          </li>
        {/each}
      </ul>
      {/if}

      {#if tree.total_items > LIMIT}
        <div class="mt-3 flex items-center gap-2">
          <button
            class="rounded-lg border border-slate-300 px-3 py-1 text-sm disabled:opacity-40 dark:border-slate-700"
            disabled={offset === 0}
            onclick={() => (offset = Math.max(0, offset - LIMIT))}>Prev</button>
          <button
            class="rounded-lg border border-slate-300 px-3 py-1 text-sm disabled:opacity-40 dark:border-slate-700"
            disabled={offset + LIMIT >= tree.total_items}
            onclick={() => (offset = offset + LIMIT)}>Next</button>
        </div>
      {/if}
    {/if}
  {/if}

  {#if selected}
    <ItemDetail id={selected} onClose={() => (selected = null)} />
  {/if}
</div>
