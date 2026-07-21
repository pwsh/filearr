<script lang="ts">
  import { copyText } from "./clipboard";
  import {
    friendlyError,
    getItem,
    itemCopies,
    similarItems,
    type ItemRecord,
    type CopiesResponse,
    type SimilarResponse,
  } from "./api";
  import RawView from "./RawView.svelte";
  import Breadcrumbs from "./Breadcrumbs.svelte";
  import { gotoBrowse } from "./routes";
  import { cardFor, cardLabel } from "./cards/registry";
  import ArchiveSection from "./ArchiveSection.svelte";
  import Thumb from "./Thumb.svelte";
  import RetrievePanel from "./RetrievePanel.svelte";
  import AgentStatusPanel from "./AgentStatusPanel.svelte";

  let { id, onClose }: { id: string; onClose: () => void } = $props();

  let item = $state<ItemRecord | null>(null);
  let error = $state("");

  // Narrow the untyped record to the breadcrumb fields (all optional).
  const str = (k: string): string | null => {
    const v = item?.[k];
    return typeof v === "string" ? v : null;
  };
  const relPath = $derived(str("rel_path") ?? "");
  const libId = $derived(str("library_id") ?? "");
  const libName = $derived(str("library_name") ?? "Library");
  // UI-T15: url-ish + UNC spellings of the library share; Breadcrumbs picks the
  // OS-appropriate one.
  const shareUrl = $derived(str("library_share_prefix"));
  const shareUnc = $derived(str("library_share_unc"));
  const nativePath = $derived(str("native_path"));
  const containerPath = $derived(str("path"));

  // P10-T11/T12: the item's own RESOLVED network location + which tier produced
  // it (agent hint > admin mapping > library share_prefix). This is the unified,
  // authoritative file-open affordance: when present it supersedes the
  // library-share_prefix "Open file" row in <Breadcrumbs> (suppressed below).
  // Null => render nothing (no fabricated location, no empty state).
  const itemShareUrl = $derived(str("share_url"));
  const shareSource = $derived(str("share_source"));
  const shareSourceLabel = $derived(
    shareSource === "agent_hint"
      ? "from agent"
      : shareSource === "mapping"
        ? "admin mapping"
        : shareSource === "library"
          ? "library share"
          : "",
  );
  let shareCopied = $state(false);
  let shareCopiedTimer: ReturnType<typeof setTimeout>;
  async function copyShare(text: string) {
    // FIX-5 plain-http-safe helper (navigator.clipboard is unavailable over
    // http://<lan-ip>); it falls back to a textarea + execCommand copy.
    await copyText(text);
    shareCopied = true;
    clearTimeout(shareCopiedTimer);
    shareCopiedTimer = setTimeout(() => (shareCopied = false), 1500);
  }

  // P4-T10: the typed per-media_type card is the FIRST tab; "Raw" is ALWAYS last.
  // The card component is resolved from the registry (a future/unregistered type
  // falls back to the generic key-facts card). Rendered via native Svelte-5
  // dynamic-component syntax below — no <svelte:component>.
  const mediaType = $derived(str("file_category") ?? "");
  const CardComponent = $derived(cardFor(mediaType));
  const cardTabLabel = $derived(cardLabel(mediaType));
  type TabId = "card" | "raw";
  let active = $state<TabId>("card");

  // P10-T3/T10: agent-hosted items (library owned by an agent) surface the
  // hosting agent's identity, online status, verify freshness, and an inline
  // Verify action via <AgentStatusPanel>. ``source_agent_id`` on the item record
  // is the cheap ownership gate; the panel fetches the live agent-status detail.
  const agentOwned = $derived(!!str("source_agent_id"));
  // P10-T6 retrieve: the download filename for the staged file.
  const fileName = $derived(str("filename") ?? relPath.split("/").pop() ?? "download");

  // Navigating a breadcrumb folder closes the modal and switches to browse.
  function navigate(folderRelPath: string) {
    onClose();
    if (libId) gotoBrowse(libId, folderRelPath);
  }

  // P3-T10: the OTHER copies of this item. Always fetched on open; the Copies
  // section renders whenever the group has more than one member (count > 1).
  let copies = $state<CopiesResponse | null>(null);
  let copiedPath = $state<string | null>(null);
  let copiedTimer: ReturnType<typeof setTimeout>;

  // P3-T9: related / near-duplicate items via the semantic vector. Lazy — nothing
  // is fetched until the user expands the section (semantic search may be off, in
  // which case the endpoint 409s and we just show an "unavailable" note).
  let similar = $state<SimilarResponse | null>(null);
  let similarOpen = $state(false);
  let similarLoaded = $state(false);
  let similarLoading = $state(false);
  let similarError = $state("");

  async function toggleSimilar() {
    similarOpen = !similarOpen;
    if (!similarOpen || similarLoaded || similarLoading) return;
    similarLoading = true;
    similarError = "";
    try {
      similar = await similarItems(id, 10);
    } catch (e) {
      similar = null;
      similarError = "Similar items are unavailable for this item.";
    } finally {
      similarLoading = false;
      similarLoaded = true;
    }
  }

  const hitLabel = (h: Record<string, unknown>): string => {
    const t = h.title ?? h.filename ?? h.rel_path ?? h.id;
    return typeof t === "string" ? t : String(t ?? "");
  };
  const hitPath = (h: Record<string, unknown>): string => {
    const v = h.path ?? h.rel_path;
    return typeof v === "string" ? v : "";
  };

  async function copyCopyPath(path: string) {
    try {
      await copyText(path);
      copiedPath = path;
      clearTimeout(copiedTimer);
      copiedTimer = setTimeout(() => (copiedPath = null), 2000);
    } catch {
      copiedPath = "(clipboard blocked)";
      clearTimeout(copiedTimer);
      copiedTimer = setTimeout(() => (copiedPath = null), 2000);
    }
  }

  $effect(() => {
    error = "";
    item = null;
    copies = null;
    // P3-T9: reset the lazy Similar section for the newly-opened item.
    similar = null;
    similarOpen = false;
    similarLoaded = false;
    similarLoading = false;
    similarError = "";
    active = "card"; // reset to the typed card whenever a different item opens
    getItem(id)
      .then((r) => (item = r))
      // RBAC (P6-T4): a 403/404 shows a friendly line, never a blank/raw dump.
      .catch((e) => (error = friendlyError(e)));
    // Copies are a separate, non-blocking fetch — a failure just hides the section.
    itemCopies(id)
      .then((r) => (copies = r))
      .catch(() => (copies = null));
  });

  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") onClose();
  }
</script>

<svelte:window onkeydown={onKey} />

<div class="fixed inset-0 z-50 overflow-y-auto">
  <!-- Backdrop: a real <button> so click-to-close carries no a11y warnings. -->
  <button
    type="button"
    class="absolute inset-0 h-full w-full cursor-default bg-black/50"
    aria-label="Close details"
    onclick={onClose}
  ></button>

  <div
    class="relative z-10 mx-auto mt-16 mb-8 w-full max-w-3xl rounded-2xl bg-white p-5 shadow-xl dark:bg-slate-900"
    role="dialog"
    aria-modal="true"
    aria-label="Item details"
  >
    {#if item}
      <div class="mb-3 border-b border-slate-200 pb-3 dark:border-slate-800">
        <Breadcrumbs
          libraryName={libName}
          {shareUrl}
          {shareUnc}
          {relPath}
          isFile={true}
          {nativePath}
          {containerPath}
          hideFileActions={!!itemShareUrl}
          onNavigate={navigate}
        />
        <!-- P10-T11/T12 unified network location: the item's own resolved
             share_url (agent hint > admin mapping > library share). Rendered ONLY
             when present; replaces the library-share "Open file" row above. -->
        {#if itemShareUrl}
          <div class="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <a
              class="min-w-0 max-w-full truncate rounded border border-slate-300 px-2 py-1 font-mono text-slate-600 hover:border-[var(--accent)] hover:text-[var(--accent)] dark:border-slate-700 dark:text-slate-300"
              href={itemShareUrl}
              target="_blank"
              rel="noopener noreferrer"
              title="Open the file at its network location. Browsers may block smb:// / file:// links — use Copy if nothing happens."
              >{itemShareUrl}</a>
            <button
              type="button"
              class="rounded border border-slate-300 px-2 py-1 text-slate-600 hover:border-[var(--accent)] hover:text-[var(--accent)] dark:border-slate-700 dark:text-slate-300"
              onclick={() => copyShare(itemShareUrl)}>{shareCopied ? "Copied!" : "Copy"}</button>
            {#if shareSourceLabel}
              <span
                class="rounded-full bg-slate-200 px-2 py-0.5 font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-400"
                title="Source of this network location">{shareSourceLabel}</span>
            {/if}
          </div>
        {/if}
      </div>
    {/if}

    {#if item}
      <!-- S12/P12 slice 1: the larger preview-tier thumbnail (lazy-generated on
           first request). Hides itself on miss (skipped type / not-yet-generated). -->
      <div class="mb-3 flex justify-center">
        <Thumb id={String(item.id)} tier="preview" size="max-h-64 w-auto" rounded="rounded-lg" />
      </div>
    {/if}

    <!-- P10-T10 agent identity + online status + inline Verify (freshness updates
         in place, no full transfer). -->
    {#if item && agentOwned}
      <div class="mb-3">
        <AgentStatusPanel itemId={id} />
      </div>
      <!-- P10-T6/T7: pull the file from the hosting agent to the server, then
           download it. Offline agents show a clear waiting state, never a spinner. -->
      <div class="mb-3 border-b border-slate-200 pb-3 dark:border-slate-800">
        <RetrievePanel itemId={id} filename={fileName} />
      </div>
    {/if}

    <div class="flex items-center gap-2 border-b border-slate-200 pb-3 dark:border-slate-800">
      <!-- Typed card tab first, Raw last. -->
      <button
        class="rounded-lg px-3 py-1 text-sm {active === 'card'
          ? 'bg-[var(--accent)] text-white'
          : 'text-slate-500'}"
        onclick={() => (active = "card")}>{cardTabLabel}</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {active === 'raw'
          ? 'bg-[var(--accent)] text-white'
          : 'text-slate-500'}"
        onclick={() => (active = "raw")}>Raw</button>
      <span class="grow"></span>
      <button
        class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
        onclick={onClose}>Close</button>
    </div>

    <div class="mt-4">
      {#if error}
        <p class="text-red-500">{error}</p>
      {:else if !item}
        <p class="text-slate-500">Loading…</p>
      {:else if active === "raw"}
        <RawView {item} />
      {:else}
        {@const Card = CardComponent}
        <Card {item} />
      {/if}
    </div>

    <!-- P3-T13 Archive contents: shown whenever this item's extracted metadata
         carries an ``archive`` fact (zip/tar member listing, index-only). -->
    {#if item}
      <ArchiveSection {item} />
    {/if}

    <!-- P3-T10 Copies section: shown whenever this item has duplicates (the group
         has more than one active member). Each row lists the owning library +
         path with a copy-path action (native_prefix-resolved, invariant 3). -->
    {#if copies && copies.count > 1}
      <div class="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
        <h3 class="mb-2 text-sm font-semibold">
          {copies.count} copies
          <span class="ml-1 font-normal text-slate-500">
            ({copies.copies.length} other{copies.copies.length === 1 ? "" : "s"}
            {copies.capped ? ", showing first 50" : ""})
          </span>
        </h3>
        <ul class="flex flex-col gap-1">
          {#each copies.copies as c (c.id)}
            {@const path = c.native_path ?? c.path}
            <li class="flex items-center gap-2 text-xs">
              <span class="shrink-0 rounded bg-slate-200 px-2 py-0.5 dark:bg-slate-800"
                >{c.library_name ?? "?"}</span>
              <span class="min-w-0 flex-1 truncate font-mono" title={path}>{path}</span>
              <button
                type="button"
                class="shrink-0 rounded border border-slate-300 px-2 py-0.5 dark:border-slate-700"
                onclick={() => copyCopyPath(path)}>Copy path</button>
            </li>
          {/each}
        </ul>
        {#if copiedPath}
          <p class="mt-2 text-xs text-[var(--accent)]" role="status">Copied {copiedPath}</p>
        {/if}
      </div>
    {/if}

    <!-- P3-T9 Similar section: lazy-loaded on expand. Hidden failure (409 when
         semantic search is off or the item is unembedded) => an "unavailable" note. -->
    {#if item}
      <div class="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
        <button
          type="button"
          class="text-sm font-semibold"
          aria-expanded={similarOpen}
          onclick={toggleSimilar}>Similar items {similarOpen ? "▲" : "▾"}</button>
        {#if similarOpen}
          <div class="mt-2">
            {#if similarLoading}
              <p class="text-xs text-slate-500">Loading…</p>
            {:else if similarError}
              <p class="text-xs text-slate-500">{similarError}</p>
            {:else if similar && similar.hits.length}
              <ul class="flex flex-col gap-1">
                {#each similar.hits as h (h.id)}
                  <li class="flex items-center gap-2 text-xs">
                    <span class="min-w-0 flex-1 truncate" title={hitPath(h)}>{hitLabel(h)}</span>
                    <span class="shrink-0 truncate font-mono text-slate-400" title={hitPath(h)}
                      >{hitPath(h)}</span>
                  </li>
                {/each}
              </ul>
            {:else}
              <p class="text-xs text-slate-500">No similar items found.</p>
            {/if}
          </div>
        {/if}
      </div>
    {/if}
  </div>
</div>
