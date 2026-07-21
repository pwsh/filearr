<script lang="ts">
  // UI-T1 — full library edit modal (same overlay pattern as ItemDetail). Exposes
  // every editable field consolidated into labelled sections: identity (name,
  // root_path + warning, native_prefix, enabled), content (types + include/exclude
  // globs), schedule (cron/watch), hashing (policy/ceiling), and the P2-T5 indexing
  // controls (presets + extension groups) that previously lived in the Admin
  // expander. Edits collect into a local draft and PATCH once on Save; a 422 body
  // surfaces inline. All dynamic strings render as text (no {@html}).
  import { untrack } from "svelte";
  import {
    updateLibrary,
    type ExtensionGroup, type HashPolicy, type Library, type Preset, type PresetsResponse,
    type TaxonomyNode,
  } from "./api";
  import { HELP } from "./help";
  import Help from "./Help.svelte";
  import { formatShare, shareLocation } from "./osFormat";
  import { shareFormat, detectedPlatform } from "./osFormat.svelte";
  import FolderPicker from "./FolderPicker.svelte";
  import ScheduleField from "./ScheduleField.svelte";
  import TaxonomySelector from "./TaxonomySelector.svelte";

  let {
    library,
    presetsMeta,
    taxonomyTree,
    onSaved,
    onClose,
  }: {
    library: Library;
    presetsMeta: PresetsResponse;
    taxonomyTree: TaxonomyNode[];
    onSaved: () => void;
    onClose: () => void;
  } = $props();

  const ALL_TYPES = [
    "video", "audio", "audiobook", "sample", "image", "model3d", "document", "spreadsheet",
  ];
  const HASH_POLICIES: HashPolicy[] = ["auto", "full", "quick_only"];

  // Working draft, seeded ONCE from the library prop. The modal is remounted per
  // edit target (keyed by `editing` in AdminPage), so capturing the initial value
  // is the intended behaviour — `untrack` makes that explicit and keeps the draft
  // fields from re-binding to the prop (silences state_referenced_locally). Globs
  // edit as newline-joined text.
  const seed = untrack(() => ({
    name: library.name,
    rootPath: library.root_path,
    nativePrefix: library.native_prefix ?? "",
    sharePrefix: library.share_prefix ?? "",
    enabled: library.enabled,
    enabledCategories: [...(library.enabled_categories ?? [])],
    enabledGroups: [...(library.enabled_groups ?? [])],
    includeGlobs: (library.include_globs ?? []).join("\n"),
    excludeGlobs: (library.exclude_globs ?? []).join("\n"),
    scanCron: library.scan_cron ?? "",
    watchMode: library.watch_mode,
    hashPolicy: library.hash_policy,
    hashCeiling: library.hash_full_max_bytes != null ? String(library.hash_full_max_bytes) : "",
    presets: [...(library.enabled_presets ?? [])],
    groups: [...(library.enabled_extension_groups ?? [])],
    ocrEnabled: library.ocr_enabled,
    exposeGps: library.expose_gps,
    countPrunedFiles: library.count_pruned_files,
  }));

  let name = $state(seed.name);
  let rootPath = $state(seed.rootPath);
  let nativePrefix = $state(seed.nativePrefix);
  let sharePrefix = $state(seed.sharePrefix);
  // UI-T15: the auto (mount-map) network location rendered in the viewer's OS
  // spelling (smb:// vs UNC), following the header format selector.
  const autoShareText = $derived(
    library.share_prefix_source === "mount-map"
      ? formatShare(
          shareLocation(library.share_prefix_effective, library.share_unc_effective),
          shareFormat.pref,
          detectedPlatform,
        )
      : null,
  );
  let enabled = $state(seed.enabled);
  // W8: taxonomy type-gating (replaces the old flat `types`). Distinct from the
  // legacy `groups` below (P2-T5 extension groups) — these are taxonomy keys.
  let enabledCategories = $state<string[]>(seed.enabledCategories);
  let enabledGroups = $state<string[]>(seed.enabledGroups);
  let includeGlobs = $state(seed.includeGlobs);
  let excludeGlobs = $state(seed.excludeGlobs);
  let scanCron = $state(seed.scanCron);
  let watchMode = $state(seed.watchMode);
  let hashPolicy = $state<HashPolicy>(seed.hashPolicy);
  let hashCeiling = $state(seed.hashCeiling);
  let presets = $state<string[]>(seed.presets);
  let groups = $state<string[]>(seed.groups);
  let ocrEnabled = $state(seed.ocrEnabled);
  let exposeGps = $state(seed.exposeGps);
  let countPrunedFiles = $state(seed.countPrunedFiles);

  let error = $state("");
  let busy = $state(false);
  let showPicker = $state(false);
  let showPatterns = $state<Record<string, boolean>>({});

  const rootChanged = $derived(rootPath !== library.root_path);

  // Mirrors presets.resolve_effective_presets: a default-on bundle is active
  // unless a `-name` opt-out sentinel is present; others only when listed.
  function isPresetActive(p: Preset): boolean {
    if (presets.includes("-" + p.name)) return false;
    if (presets.includes(p.name)) return true;
    return p.default_enabled;
  }

  function togglePreset(p: Preset) {
    const active = isPresetActive(p);
    let next = presets.filter((e) => e !== p.name && e !== "-" + p.name);
    if (active && p.default_enabled) next = [...next, "-" + p.name];
    else if (!active && !p.default_enabled) next = [...next, p.name];
    presets = next;
  }

  function toggleGroup(name: string) {
    groups = groups.includes(name) ? groups.filter((g) => g !== name) : [...groups, name];
  }

  function groupsForType(mt: string): ExtensionGroup[] {
    return presetsMeta.extension_groups.filter((g) => g.file_category === mt);
  }

  function linesToArray(s: string): string[] {
    return s.split("\n").map((l) => l.trim()).filter(Boolean);
  }

  async function save() {
    busy = true;
    error = "";
    try {
      await updateLibrary(library.id, {
        name,
        root_path: rootPath,
        native_prefix: nativePrefix.trim() ? nativePrefix.trim() : null,
        share_prefix: sharePrefix.trim() ? sharePrefix.trim() : null,
        enabled,
        enabled_categories: enabledCategories,
        enabled_groups: enabledGroups,
        include_globs: linesToArray(includeGlobs),
        exclude_globs: linesToArray(excludeGlobs),
        scan_cron: scanCron.trim() ? scanCron.trim() : null,
        watch_mode: watchMode,
        hash_policy: hashPolicy,
        hash_full_max_bytes: hashCeiling.trim() ? Number(hashCeiling) : null,
        enabled_presets: presets,
        enabled_extension_groups: groups,
        ocr_enabled: ocrEnabled,
        expose_gps: exposeGps,
        count_pruned_files: countPrunedFiles,
      });
      onSaved();
    } catch (e) {
      // 422s (invalid cron, network watch path, unknown preset/group, non-positive
      // ceiling, …) arrive as "422: <detail>" and are shown inline.
      error = String(e);
    } finally {
      busy = false;
    }
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape" && !showPicker) onClose();
  }
</script>

<svelte:window onkeydown={onKey} />

<div class="fixed inset-0 z-50 overflow-y-auto">
  <button
    type="button"
    class="absolute inset-0 h-full w-full cursor-default bg-black/50"
    aria-label="Close editor"
    onclick={onClose}
  ></button>

  <div
    class="relative z-10 mx-auto mt-12 mb-8 w-full max-w-2xl rounded-2xl bg-white p-5 shadow-xl dark:bg-slate-900"
    role="dialog"
    aria-modal="true"
    aria-label="Edit library"
  >
    <div class="flex items-center gap-2 border-b border-slate-200 pb-3 dark:border-slate-800">
      <h3 class="text-base font-semibold">Edit library</h3>
      <span class="grow"></span>
      <button
        class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
        onclick={onClose}>Close</button>
    </div>

    {#if error}<p class="mt-3 text-sm text-red-500">{error}</p>{/if}

    <div class="mt-4 space-y-6 text-sm">
      <!-- Identity -->
      <section>
        <h4 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Identity</h4>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-[10rem_1fr] sm:items-start">
          <label class="flex items-center gap-1 pt-2 text-slate-600 dark:text-slate-300">
            Name <Help text={HELP.name} label="name" />
          </label>
          <input class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
            bind:value={name} />

          <label class="flex items-center gap-1 pt-2 text-slate-600 dark:text-slate-300">
            Root path <Help text={HELP.root_path} label="root path" />
          </label>
          <div>
            <div class="flex gap-2">
              <input class="grow rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono dark:border-slate-700"
                bind:value={rootPath} />
              <button type="button"
                class="rounded-lg border border-slate-300 px-3 py-2 text-xs dark:border-slate-700"
                onclick={() => (showPicker = true)}>Browse…</button>
            </div>
            {#if rootChanged}
              <p class="mt-1 text-xs text-amber-600 dark:text-amber-400">
                Identity is (library_id, rel_path); moving the root re-anchors rel_paths — the next
                scan treats unchanged rel_paths as the same items, but a different tree shape will
                tombstone/move items.
              </p>
            {/if}
          </div>

          <label class="flex items-center gap-1 pt-2 text-slate-600 dark:text-slate-300">
            Native prefix <Help text={HELP.native_prefix} label="native prefix" />
          </label>
          <input class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono dark:border-slate-700"
            placeholder="(optional) e.g. /mnt/user/media" bind:value={nativePrefix} />

          <label class="flex items-center gap-1 pt-2 text-slate-600 dark:text-slate-300">
            Share location <Help text={HELP.share_prefix} label="share location" />
          </label>
          <input class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono dark:border-slate-700"
            placeholder={library.share_prefix_source === "mount-map" && library.share_prefix_effective
              ? `auto from mount: ${library.share_prefix_effective}`
              : "(optional) e.g. \\tower\media, smb://tower/media, /Volumes/media"}
            bind:value={sharePrefix} />
          {#if library.share_prefix_source === "mount-map" && library.share_prefix_effective && !sharePrefix.trim()}
            <p class="text-xs text-slate-500">
              Auto-detected from the deploy mount map:
              <span class="font-mono text-[var(--accent)]">{library.share_prefix_effective}</span>.
              Leave blank to use it, or type a value to override.
            </p>
          {/if}

          <label class="flex items-center gap-1 text-slate-600 dark:text-slate-300">
            Enabled <Help text={HELP.enabled} label="enabled" />
          </label>
          <label class="inline-flex items-center gap-2">
            <input type="checkbox" bind:checked={enabled} />
            <span class="text-slate-500">library participates in scheduled scans</span>
          </label>
        </div>
      </section>

      <!-- Content -->
      <section>
        <h4 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Content</h4>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-[10rem_1fr] sm:items-start">
          <label class="flex items-center gap-1 pt-2 text-slate-600 dark:text-slate-300">
            File types <Help text={HELP.media_types} label="file types" />
          </label>
          <TaxonomySelector tree={taxonomyTree} bind:categories={enabledCategories} bind:groups={enabledGroups} />

          <label class="flex items-center gap-1 pt-2 text-slate-600 dark:text-slate-300">
            Include globs <Help text={HELP.include_globs} label="include globs" />
          </label>
          <textarea rows="2"
            class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700"
            placeholder="one glob per line (blank = include all)" bind:value={includeGlobs}></textarea>

          <label class="flex items-center gap-1 pt-2 text-slate-600 dark:text-slate-300">
            Exclude globs <Help text={HELP.exclude_globs} label="exclude globs" />
          </label>
          <textarea rows="2"
            class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700"
            placeholder="one glob per line" bind:value={excludeGlobs}></textarea>
        </div>
      </section>

      <!-- Schedule -->
      <section>
        <h4 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Schedule</h4>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-[10rem_1fr] sm:items-start">
          <label class="flex items-center gap-1 pt-1 text-slate-600 dark:text-slate-300">
            Scan schedule <Help text={HELP.scan_cron} label="scan schedule" />
          </label>
          <ScheduleField value={scanCron || null} onChange={(c) => (scanCron = c ?? "")} />

          <label class="flex items-center gap-1 text-slate-600 dark:text-slate-300">
            Watch mode <Help text={HELP.watch_mode} label="watch mode" />
          </label>
          <label class="inline-flex items-center gap-2">
            <input type="checkbox" bind:checked={watchMode} />
            <span class="text-slate-500">watch filesystem (local paths only)</span>
          </label>
        </div>
      </section>

      <!-- Hashing -->
      <section>
        <h4 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Hashing</h4>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-[10rem_1fr] sm:items-center">
          <label class="flex items-center gap-1 text-slate-600 dark:text-slate-300">
            Policy <Help text={HELP.hash_policy} label="hash policy" />
          </label>
          <select class="w-48 rounded-lg border border-slate-300 bg-transparent px-2 py-2 dark:border-slate-700"
            bind:value={hashPolicy}>
            {#each HASH_POLICIES as hp}
              <option value={hp}>{hp === "quick_only" ? "quick only" : hp}</option>
            {/each}
          </select>

          <label class="flex items-center gap-1 text-slate-600 dark:text-slate-300">
            Full-hash ceiling <Help text={HELP.hash_ceiling} label="hash ceiling" />
          </label>
          <input class="w-56 rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-xs dark:border-slate-700"
            type="number" min="1" placeholder="bytes (blank = global default)" bind:value={hashCeiling} />
        </div>
      </section>

      <!-- Content processing (OCR + privacy) -->
      <section>
        <h4 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Content processing</h4>
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-[10rem_1fr] sm:items-start">
          <label class="flex items-center gap-1 text-slate-600 dark:text-slate-300">
            OCR text <Help text={HELP.ocr_enabled} label="OCR" />
          </label>
          <label class="inline-flex items-start gap-2">
            <input type="checkbox" class="mt-1" bind:checked={ocrEnabled} />
            <span class="text-slate-500">
              OCR images &amp; scanned PDFs so their text is searchable (CPU-heavy; off by default).
            </span>
          </label>

          <label class="flex items-center gap-1 text-slate-600 dark:text-slate-300">
            Expose GPS <Help text={HELP.expose_gps} label="expose GPS" />
          </label>
          <label class="inline-flex items-start gap-2">
            <input type="checkbox" class="mt-1" bind:checked={exposeGps} />
            <span class="text-slate-500">
              Show photo GPS/location in search &amp; API. Hidden by default for privacy (CWE-1230).
            </span>
          </label>

          <!-- A span, not a <label>: the real label is the one wrapping the
               checkbox below. (The sibling rows get away with <label> only
               because a <Help> component inside suppresses the a11y check.) -->
          <span class="flex items-center gap-1 text-slate-600 dark:text-slate-300">
            Count pruned
          </span>
          <label class="inline-flex items-start gap-2">
            <input type="checkbox" class="mt-1" bind:checked={countPrunedFiles} />
            <span class="text-slate-500">
              Count files inside pruned folders (e.g. <code>.git</code>, <code>.venv</code>)
              so the scan's <b>seen + excluded + pruned</b> adds up to the file count your OS
              reports. Those folders are normally skipped without being read at all, which is
              why a library can show far fewer files than the folder's properties. Costs an
              extra directory listing per pruned folder — slow over SMB/rclone, so leave it off
              unless you are investigating a mismatch.
            </span>
          </label>
        </div>
      </section>

      <!-- Presets -->
      <section>
        <h4 class="mb-2 flex items-center gap-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Preset exclusions <Help text={HELP.presets} label="presets" />
        </h4>
        <div class="flex flex-col gap-2">
          {#each presetsMeta.presets as p (p.name)}
            {@const active = isPresetActive(p)}
            <div>
              <label class="inline-flex items-center gap-2">
                <input type="checkbox" checked={active} onchange={() => togglePreset(p)} />
                <span class="font-medium">{p.label}</span>
                {#if p.default_enabled}<span class="text-slate-400">(default on)</span>{/if}
                <button type="button" class="text-[var(--accent)] underline"
                  onclick={() => (showPatterns = { ...showPatterns, [p.name]: !showPatterns[p.name] })}>
                  {showPatterns[p.name] ? "hide" : "show"} patterns
                </button>
              </label>
              {#if p.name === "hidden_dotfiles" && !active}
                <span class="ml-6 block text-xs text-amber-600 dark:text-amber-400">
                  Warning: disabling will surface previously hidden files on the next scan.
                </span>
              {:else if p.caveat}
                <span class="ml-6 block text-xs text-slate-400">{p.caveat}</span>
              {/if}
              {#if showPatterns[p.name]}
                <code class="ml-6 block whitespace-pre-wrap font-mono text-xs text-slate-500">{p.patterns.join("  ")}</code>
              {/if}
            </div>
          {/each}
        </div>
      </section>

      <!-- Extension groups -->
      <section>
        <h4 class="mb-1 flex items-center gap-1 text-xs font-semibold uppercase tracking-wide text-slate-500">
          Extension groups <Help text={HELP.extension_groups} label="extension groups" />
        </h4>
        <p class="mb-2 text-xs text-slate-400">
          Enabling a group narrows that media type to the listed extensions (union of enabled
          groups). No group = all extensions of the type.
        </p>
        {#each ALL_TYPES as mt}
          {@const gs = groupsForType(mt)}
          {#if gs.length}
            <div class="mb-1 text-xs">
              <span class="font-medium">{mt}</span>
              <span class="ml-2 inline-flex flex-wrap gap-3">
                {#each gs as g (g.name)}
                  <label class="inline-flex items-center gap-1" title={g.extensions.join(", ")}>
                    <input type="checkbox" checked={groups.includes(g.name)} onchange={() => toggleGroup(g.name)} />
                    {g.label}
                  </label>
                {/each}
              </span>
            </div>
          {/if}
        {/each}
      </section>
    </div>

    <div class="mt-6 flex justify-end gap-2 border-t border-slate-200 pt-4 dark:border-slate-800">
      <button class="rounded-lg border border-slate-300 px-4 py-2 text-sm dark:border-slate-700"
        onclick={onClose}>Cancel</button>
      <button class="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm text-white disabled:opacity-50"
        disabled={busy} onclick={save}>{busy ? "Saving…" : "Save changes"}</button>
    </div>
  </div>
</div>

{#if showPicker}
  <FolderPicker
    initial={rootPath}
    onPick={(p) => { rootPath = p; showPicker = false; }}
    onClose={() => (showPicker = false)}
  />
{/if}
