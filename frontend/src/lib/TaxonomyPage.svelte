<script lang="ts">
  // W8 — the file-extension similarity taxonomy EDITOR. A dedicated #/taxonomy
  // route reachable from the Admin page. Shows the category -> group -> extension
  // tree and supports full CRUD: create/rename/delete categories (with an
  // extractor), create/rename/delete/REPARENT groups, and add/remove/MOVE
  // extensions (the add-to-group endpoint upserts, so adding an ext that already
  // lives elsewhere reparents it and reports where it moved from).
  //
  // Mutations are admin-scoped server-side; non-admins get a read-only view. Every
  // mutation echoes the bumped schema ``version`` (shown in the header). 409
  // (delete category with groups) and 422 (bad ext charset) surface inline.
  import { onMount } from "svelte";
  import {
    ApiError,
    getTaxonomy,
    createTaxonomyCategory,
    updateTaxonomyCategory,
    deleteTaxonomyCategory,
    createTaxonomyGroup,
    updateTaxonomyGroup,
    deleteTaxonomyGroup,
    addTaxonomyExtension,
    deleteTaxonomyExtension,
    TAXONOMY_EXTRACTORS,
    type TaxonomyNode,
    type AuthPrincipal,
  } from "./api";

  let { me = null, authDisabled = false }: { me?: AuthPrincipal | null; authDisabled?: boolean } =
    $props();
  // Auth off (dev) => unrestricted API, so treat as admin; else require the role.
  const isAdmin = $derived(authDisabled || (!!me && me.global_role === "admin"));

  let tree = $state<TaxonomyNode[]>([]);
  let version = $state<number | null>(null);
  let error = $state("");
  let loading = $state(true);

  // Per-group extension add: input text + inline error / success notice.
  let extInput = $state<Record<string, string>>({});
  let extError = $state<Record<string, string>>({});
  let extNotice = $state<Record<string, string>>({});

  function errDetail(e: unknown): string {
    if (e instanceof ApiError) {
      try {
        const j = JSON.parse(e.body);
        if (j?.detail) return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch {
        /* body not JSON */
      }
      return e.body || String(e);
    }
    return String(e);
  }

  async function refresh() {
    error = "";
    try {
      const t = await getTaxonomy();
      tree = t.tree;
      version = t.version;
    } catch (e) {
      error = errDetail(e);
    } finally {
      loading = false;
    }
  }

  onMount(refresh);

  const allCategoryKeys = $derived(tree.map((n) => n.category.key));

  // ---- category / group dialog ---------------------------------------------
  type Dialog =
    | { kind: "category"; mode: "create" | "edit"; key: string; label: string; description: string; extractor: string; sortOrder: string }
    | { kind: "group"; mode: "create" | "edit"; key: string; label: string; description: string; categoryKey: string; sortOrder: string };
  let dialog = $state<Dialog | null>(null);
  let dialogError = $state("");
  let dialogBusy = $state(false);

  function newCategory() {
    dialogError = "";
    dialog = { kind: "category", mode: "create", key: "", label: "", description: "", extractor: "none", sortOrder: "" };
  }
  function editCategory(n: TaxonomyNode) {
    dialogError = "";
    dialog = {
      kind: "category", mode: "edit",
      key: n.category.key, label: n.category.label, description: n.category.description ?? "",
      extractor: n.category.extractor || "none", sortOrder: String(n.category.sort_order ?? ""),
    };
  }
  function newGroup(categoryKey: string) {
    dialogError = "";
    dialog = { kind: "group", mode: "create", key: "", label: "", description: "", categoryKey, sortOrder: "" };
  }
  function editGroup(categoryKey: string, g: TaxonomyNode["groups"][number]) {
    dialogError = "";
    dialog = {
      kind: "group", mode: "edit",
      key: g.key, label: g.label, description: g.description ?? "",
      categoryKey, sortOrder: String(g.sort_order ?? ""),
    };
  }

  function sortNum(s: string): number | undefined {
    const t = s.trim();
    if (!t) return undefined;
    const n = Number(t);
    return Number.isFinite(n) ? n : undefined;
  }

  async function saveDialog() {
    if (!dialog) return;
    if (dialog.mode === "create" && !dialog.key.trim()) {
      dialogError = "Key is required.";
      return;
    }
    if (!dialog.label.trim()) {
      dialogError = "Label is required.";
      return;
    }
    dialogBusy = true;
    dialogError = "";
    try {
      if (dialog.kind === "category") {
        if (dialog.mode === "create") {
          await createTaxonomyCategory({
            key: dialog.key.trim(),
            label: dialog.label.trim(),
            description: dialog.description.trim() || undefined,
            extractor: dialog.extractor,
            sort_order: sortNum(dialog.sortOrder),
          });
        } else {
          await updateTaxonomyCategory(dialog.key, {
            label: dialog.label.trim(),
            description: dialog.description.trim(),
            extractor: dialog.extractor,
            ...(sortNum(dialog.sortOrder) != null ? { sort_order: sortNum(dialog.sortOrder) } : {}),
          });
        }
      } else {
        if (dialog.mode === "create") {
          await createTaxonomyGroup({
            key: dialog.key.trim(),
            label: dialog.label.trim(),
            description: dialog.description.trim() || undefined,
            category_key: dialog.categoryKey,
            sort_order: sortNum(dialog.sortOrder),
          });
        } else {
          // A changed category_key REPARENTS the group under a new category.
          await updateTaxonomyGroup(dialog.key, {
            label: dialog.label.trim(),
            description: dialog.description.trim(),
            category_key: dialog.categoryKey,
            ...(sortNum(dialog.sortOrder) != null ? { sort_order: sortNum(dialog.sortOrder) } : {}),
          });
        }
      }
      dialog = null;
      await refresh();
    } catch (e) {
      // 409 (dup key / delete-blocked) or 422 (bad key/extractor) inline.
      dialogError = errDetail(e);
    } finally {
      dialogBusy = false;
    }
  }

  async function removeCategory(n: TaxonomyNode) {
    if (!confirm(`Delete category "${n.category.label}"?`)) return;
    error = "";
    try {
      await deleteTaxonomyCategory(n.category.key);
      await refresh();
    } catch (e) {
      // 409 when the category still has groups — surface the server's message.
      error = errDetail(e);
    }
  }

  async function removeGroup(g: TaxonomyNode["groups"][number]) {
    if (!confirm(`Delete group "${g.label}"? Its ${g.extensions.length} extension(s) will be removed from the taxonomy.`)) return;
    error = "";
    try {
      await deleteTaxonomyGroup(g.key);
      await refresh();
    } catch (e) {
      error = errDetail(e);
    }
  }

  // ---- extensions -----------------------------------------------------------
  async function addExt(groupKey: string) {
    const raw = (extInput[groupKey] ?? "").trim().toLowerCase().replace(/^\./, "");
    extError = { ...extError, [groupKey]: "" };
    extNotice = { ...extNotice, [groupKey]: "" };
    if (!raw) return;
    try {
      const r = await addTaxonomyExtension(groupKey, raw);
      extInput = { ...extInput, [groupKey]: "" };
      // Upsert semantics: report a reparent so the move is visible.
      if (r.previous_group && r.previous_group !== groupKey) {
        extNotice = { ...extNotice, [groupKey]: `Moved "${raw}" from ${r.previous_group}` };
      }
      await refresh();
    } catch (e) {
      // 422 on a bad ext (must match ^[a-z0-9_+-]{1,32}$) surfaces here.
      extError = { ...extError, [groupKey]: errDetail(e) };
    }
  }

  async function removeExt(ext: string) {
    error = "";
    try {
      await deleteTaxonomyExtension(ext);
      await refresh();
    } catch (e) {
      error = errDetail(e);
    }
  }

  function goBack() {
    location.hash = "#/admin";
  }
</script>

<div class="mt-4">
  <div class="flex flex-wrap items-center gap-3">
    <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm text-slate-600 dark:border-slate-700 dark:text-slate-300"
      onclick={goBack}>← Admin</button>
    <h2 class="text-lg font-semibold">File taxonomy</h2>
    {#if version != null}
      <span class="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-500 dark:bg-slate-800" title="Schema version (bumps on every change)">v{version}</span>
    {/if}
    <span class="text-xs text-slate-500">category → group → extensions</span>
    <div class="grow"></div>
    <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm text-slate-600 dark:border-slate-700 dark:text-slate-300"
      onclick={refresh}>Refresh</button>
    {#if isAdmin}
      <button class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white" onclick={newCategory}>New category</button>
    {/if}
  </div>

  {#if !isAdmin}
    <p class="mt-2 rounded-lg border border-amber-300 bg-amber-50 p-2 text-xs text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300">
      Read-only — editing the taxonomy requires an admin account.
    </p>
  {/if}
  {#if error}<p class="mt-2 text-sm text-red-600">{error}</p>{/if}

  {#if loading}
    <p class="mt-4 text-sm text-slate-400">Loading…</p>
  {:else if tree.length === 0}
    <p class="mt-4 text-sm text-slate-400">
      No categories yet.{#if isAdmin} Use “New category” to start building the taxonomy.{/if}
    </p>
  {:else}
    <div class="mt-4 flex flex-col gap-4">
      {#each tree as node (node.category.key)}
        {@const cat = node.category}
        <div class="rounded-xl border border-slate-200 dark:border-slate-800">
          <!-- Category header -->
          <div class="flex flex-wrap items-center gap-2 border-b border-slate-100 px-4 py-3 dark:border-slate-800/70">
            <span class="text-base font-semibold">{cat.label}</span>
            <span class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-500 dark:bg-slate-800">{cat.key}</span>
            <span class="rounded bg-[var(--accent)]/15 px-1.5 py-0.5 text-[10px] text-[var(--accent)]" title="extractor family">{cat.extractor || "none"}</span>
            {#if cat.is_builtin}<span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-400 dark:bg-slate-800">built-in</span>{/if}
            <span class="text-[11px] text-slate-400">sort {cat.sort_order}</span>
            {#if cat.description}<span class="hidden text-xs text-slate-400 sm:inline">— {cat.description}</span>{/if}
            <div class="grow"></div>
            {#if isAdmin}
              <button class="text-xs text-[var(--accent)]" onclick={() => newGroup(cat.key)}>+ group</button>
              <button class="text-xs text-[var(--accent)]" onclick={() => editCategory(node)}>edit</button>
              <button class="text-xs text-red-600" onclick={() => removeCategory(node)}>delete</button>
            {/if}
          </div>

          <!-- Groups -->
          {#if node.groups.length === 0}
            <p class="px-4 py-3 text-xs text-slate-400">No groups in this category.</p>
          {:else}
            <div class="divide-y divide-slate-100 dark:divide-slate-800/70">
              {#each node.groups as g (g.key)}
                <div class="px-4 py-3">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="font-medium">{g.label}</span>
                    <span class="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-500 dark:bg-slate-800">{g.key}</span>
                    {#if g.is_builtin}<span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-400 dark:bg-slate-800">built-in</span>{/if}
                    <span class="text-[11px] text-slate-400">sort {g.sort_order}</span>
                    {#if g.description}<span class="hidden text-xs text-slate-400 sm:inline">— {g.description}</span>{/if}
                    <div class="grow"></div>
                    {#if isAdmin}
                      <button class="text-xs text-[var(--accent)]" onclick={() => editGroup(cat.key, g)} title="Rename or reparent (change category)">edit / reparent</button>
                      <button class="text-xs text-red-600" onclick={() => removeGroup(g)}>delete</button>
                    {/if}
                  </div>

                  <!-- Extensions -->
                  <div class="mt-2 flex flex-wrap items-center gap-1.5">
                    {#if g.extensions.length === 0}
                      <span class="text-xs text-slate-400">no extensions</span>
                    {:else}
                      {#each g.extensions as ext (ext)}
                        <span class="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 font-mono text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                          .{ext}
                          {#if isAdmin}
                            <button class="rounded-full px-0.5 leading-none text-slate-400 hover:text-red-600"
                              aria-label={`Remove .${ext}`} title="Remove extension" onclick={() => removeExt(ext)}>×</button>
                          {/if}
                        </span>
                      {/each}
                    {/if}
                  </div>

                  {#if isAdmin}
                    <!-- Add / move an extension. Adding one that lives in another
                         group reparents it here (upsert) and reports the move. -->
                    <div class="mt-2 flex flex-wrap items-center gap-2">
                      <input
                        class="w-40 rounded border border-slate-300 bg-transparent px-2 py-1 font-mono text-xs outline-none focus:border-[var(--accent)] dark:border-slate-700"
                        placeholder="add ext (e.g. mkv)"
                        value={extInput[g.key] ?? ""}
                        oninput={(e) => (extInput = { ...extInput, [g.key]: (e.currentTarget as HTMLInputElement).value })}
                        onkeydown={(e) => { if (e.key === "Enter") { e.preventDefault(); addExt(g.key); } }} />
                      <button class="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" onclick={() => addExt(g.key)}>add</button>
                      {#if extError[g.key]}<span class="text-xs text-red-600">{extError[g.key]}</span>{/if}
                      {#if extNotice[g.key]}<span class="text-xs text-emerald-600">{extNotice[g.key]}</span>{/if}
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</div>

<!-- Create / edit dialog (category or group) -->
{#if dialog}
  <div class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4">
    <div class="my-8 w-full max-w-lg rounded-xl border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-700 dark:bg-slate-900">
      <div class="flex items-center gap-3">
        <h3 class="text-lg font-semibold">
          {dialog.mode === "create" ? "New" : "Edit"} {dialog.kind}
        </h3>
        <div class="grow"></div>
        <button class="text-slate-500" onclick={() => (dialog = null)}>✕</button>
      </div>

      {#if dialogError}
        <p class="mt-2 rounded-lg border border-red-300 bg-red-50 p-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">{dialogError}</p>
      {/if}

      <div class="mt-3 flex flex-col gap-3">
        <label class="text-xs text-slate-500">Key {#if dialog.mode === "edit"}<span class="text-slate-400">(immutable)</span>{/if}
          <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-sm disabled:opacity-60 dark:border-slate-700"
            placeholder="lowercase-key" disabled={dialog.mode === "edit"} bind:value={dialog.key} />
        </label>
        <label class="text-xs text-slate-500">Label
          <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700" bind:value={dialog.label} />
        </label>
        <label class="text-xs text-slate-500">Description
          <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700" bind:value={dialog.description} />
        </label>

        {#if dialog.kind === "category"}
          <label class="text-xs text-slate-500">Extractor
            <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800" bind:value={dialog.extractor}>
              {#each TAXONOMY_EXTRACTORS as ex}
                <option value={ex}>{ex}</option>
              {/each}
            </select>
          </label>
        {:else}
          <label class="text-xs text-slate-500">Category {#if dialog.mode === "edit"}<span class="text-slate-400">(change to reparent)</span>{/if}
            <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800" bind:value={dialog.categoryKey}>
              {#each allCategoryKeys as ck}
                <option value={ck}>{ck}</option>
              {/each}
            </select>
          </label>
        {/if}

        <label class="text-xs text-slate-500">Sort order
          <input type="number" class="mt-1 block w-32 rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700"
            placeholder="(auto)" bind:value={dialog.sortOrder} />
        </label>
      </div>

      <div class="mt-4 flex justify-end gap-2">
        <button class="rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700" onclick={() => (dialog = null)}>Cancel</button>
        <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white disabled:opacity-50" disabled={dialogBusy} onclick={saveDialog}>
          {dialogBusy ? "Saving…" : dialog.mode === "create" ? "Create" : "Save"}
        </button>
      </div>
    </div>
  </div>
{/if}
