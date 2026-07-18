<script lang="ts">
  import { copyText } from "./clipboard";
  // UI-T12 — shared path breadcrumb used by BOTH the item detail view (Part 2)
  // and the in-page browse view (Part 3). Renders: library root + each rel_path
  // directory segment + (for a file) the filename. Directory segments navigate
  // in-page via `onNavigate`; every segment carries a small menu with
  // "Open via network" (a file:// / smb:// link, only when share_prefix is set)
  // and "Copy path". A file additionally gets Open file / Open folder / Copy
  // path actions.
  //
  // ALL dynamic names render as TEXT (Svelte auto-escapes) — names are untrusted.
  // HONESTY: browsers commonly BLOCK file:// navigation from an http page, so
  // every open link is paired with a copy button; see help.ts open_location.
  import { buildOpenUrl, buildDisplayPath, relSegments } from "./pathlinks";
  import { formatShare, shareLocation } from "./osFormat";
  import { shareFormat, detectedPlatform } from "./osFormat.svelte";

  let {
    libraryName,
    shareUrl = null,
    shareUnc = null,
    relPath,
    isFile = false,
    nativePath = null,
    containerPath = null,
    hideFileActions = false,
    onNavigate,
  }: {
    libraryName: string;
    // UI-T15: both OS spellings of the library's network location. The effective
    // one (per the viewer's OS / manual override) is chosen reactively below, so
    // every Open/Copy affordance flips the instant the format selector changes.
    shareUrl?: string | null;
    shareUnc?: string | null;
    relPath: string;
    isFile?: boolean;
    nativePath?: string | null;
    containerPath?: string | null;
    // P10-T11/T12: when the caller renders its own (item-level) resolved network
    // affordance, suppress this component's duplicate file open/copy row so the
    // two don't compete. Folder-segment path menus are unaffected (there is no
    // item-level per-folder equivalent). Defaults false (browse view unchanged).
    hideFileActions?: boolean;
    onNavigate: (folderRelPath: string) => void;
  } = $props();

  const sharePrefix = $derived(
    formatShare(shareLocation(shareUrl, shareUnc), shareFormat.pref, detectedPlatform),
  );

  type Crumb = { label: string; relTo: string; isDir: boolean };

  const crumbs = $derived.by<Crumb[]>(() => {
    const segs = relSegments(relPath);
    const out: Crumb[] = [{ label: libraryName, relTo: "", isDir: true }];
    segs.forEach((seg, i) => {
      out.push({
        label: seg,
        relTo: segs.slice(0, i + 1).join("/"),
        isDir: isFile ? i < segs.length - 1 : true,
      });
    });
    return out;
  });

  // Directory of a file = its rel_path minus the filename ('' if at root).
  const fileDir = $derived(relSegments(relPath).slice(0, -1).join("/"));

  let openMenu = $state<number | null>(null);
  let copied = $state<string | null>(null);
  let copyTimer: ReturnType<typeof setTimeout>;

  function toggleMenu(i: number) {
    openMenu = openMenu === i ? null : i;
  }

  async function copy(label: string, textToCopy: string) {
    try {
      await copyText(textToCopy);
    } catch {
      // Fallback for insecure contexts / older browsers.
      const ta = document.createElement("textarea");
      ta.value = textToCopy;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } catch {
        /* give up silently */
      }
      document.body.removeChild(ta);
    }
    copied = label;
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => (copied = null), 1200);
    openMenu = null;
  }

  // Copy target for a segment: the display path when a share prefix is set, else
  // the plain rel_path up to that segment.
  function segCopyText(relTo: string): string {
    return sharePrefix ? buildDisplayPath(sharePrefix, relTo) : relTo;
  }

  // Copy target for the file itself: share display path, else native, else the
  // in-container path, else the bare rel_path (spec fallback order).
  function fileCopyText(): string {
    if (sharePrefix) return buildDisplayPath(sharePrefix, relPath);
    return nativePath ?? containerPath ?? relPath;
  }
</script>

<div class="text-sm">
  <div class="flex flex-wrap items-center gap-x-0.5 gap-y-1">
    {#each crumbs as c, i (i)}
      {#if i > 0}<span class="px-0.5 text-slate-400">/</span>{/if}
      <span class="relative inline-flex items-center">
        {#if c.isDir}
          <button
            type="button"
            class="max-w-[16rem] truncate rounded px-1 py-0.5 text-[var(--accent)] hover:bg-slate-100 hover:underline dark:hover:bg-slate-800"
            title="Browse {c.label}"
            onclick={() => onNavigate(c.relTo)}>{c.label}</button>
        {:else}
          <span class="max-w-[20rem] truncate px-1 py-0.5 font-medium" title={c.label}>{c.label}</span>
        {/if}
        <button
          type="button"
          class="rounded px-1 text-xs text-slate-400 hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800"
          aria-label="Path actions for {c.label}"
          title="Path actions"
          onclick={() => toggleMenu(i)}>⋯</button>

        {#if openMenu === i}
          <div
            class="absolute left-0 top-full z-20 mt-1 w-max min-w-[10rem] rounded-lg border border-slate-200 bg-white p-1 text-xs shadow-lg dark:border-slate-700 dark:bg-slate-800"
            role="menu">
            {#if sharePrefix}
              <a
                class="block rounded px-2 py-1 hover:bg-slate-100 dark:hover:bg-slate-700"
                href={buildOpenUrl(sharePrefix, c.relTo)}
                target="_blank"
                rel="noopener noreferrer"
                title="Opens the network location. Browsers may block file:// links — use Copy path if nothing happens."
                onclick={() => (openMenu = null)}>Open via network</a>
            {/if}
            <button
              type="button"
              class="block w-full rounded px-2 py-1 text-left hover:bg-slate-100 dark:hover:bg-slate-700"
              onclick={() => copy(`seg-${i}`, segCopyText(c.relTo))}>
              {copied === `seg-${i}` ? "Copied!" : "Copy path"}
            </button>
          </div>
        {/if}
      </span>
    {/each}
  </div>

  {#if isFile && !hideFileActions}
    <div class="mt-2 flex flex-wrap items-center gap-2 text-xs">
      {#if sharePrefix}
        <a
          class="rounded border border-slate-300 px-2 py-1 text-slate-600 hover:border-[var(--accent)] hover:text-[var(--accent)] dark:border-slate-700 dark:text-slate-300"
          href={buildOpenUrl(sharePrefix, relPath)}
          target="_blank"
          rel="noopener noreferrer"
          title="Open the file at its network location. Browsers may block file:// — use Copy path if nothing happens.">
          Open file
        </a>
        <a
          class="rounded border border-slate-300 px-2 py-1 text-slate-600 hover:border-[var(--accent)] hover:text-[var(--accent)] dark:border-slate-700 dark:text-slate-300"
          href={buildOpenUrl(sharePrefix, fileDir)}
          target="_blank"
          rel="noopener noreferrer"
          title="Open the containing folder. Browsers may block file:// — use Copy path if nothing happens.">
          Open folder
        </a>
      {/if}
      <button
        type="button"
        class="rounded border border-slate-300 px-2 py-1 text-slate-600 hover:border-[var(--accent)] hover:text-[var(--accent)] dark:border-slate-700 dark:text-slate-300"
        onclick={() => copy("file", fileCopyText())}>
        {copied === "file" ? "Copied!" : "Copy path"}
      </button>
      {#if !sharePrefix}
        <span class="text-slate-400">Set a share location on the library to enable Open links.</span>
      {/if}
    </div>
  {/if}
</div>
