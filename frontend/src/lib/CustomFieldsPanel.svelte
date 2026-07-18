<script lang="ts">
  // P4-T3 — admin surface for custom-field DEFINITIONS. List + create + edit +
  // soft-delete. Values written under these keys live in items.user_metadata and
  // are type-checked on edit (P4-T4); this panel only manages the definitions.
  //
  // name + data_type are IMMUTABLE server-side (the API 422s a change), so the
  // edit form exposes only the mutable fields. facetable/sortable show a note
  // that they take effect after an index rebuild (P4-T6). All dynamic strings
  // render as text (no {@html}).
  import { onMount } from "svelte";
  import {
    CUSTOM_FIELD_TYPES,
    createCustomField,
    deleteCustomField,
    listCustomFields,
    updateCustomField,
    type CustomField,
    type CustomFieldType,
    type Library,
  } from "./api";
  import { HELP } from "./help";
  import Help from "./Help.svelte";

  let { libraries = [] }: { libraries?: Library[] } = $props();

  let fields = $state<CustomField[]>([]);
  let error = $state("");
  let loading = $state(true);

  // create form
  let cName = $state("");
  let cLabel = $state("");
  let cType = $state<CustomFieldType>("string");
  let cLibraryIds = $state<string[]>([]);
  let cFacetable = $state(false);
  let cSortable = $state(false);
  let cRequired = $state(false);
  let cSelectOptions = $state(""); // newline-joined, only for select
  let creating = $state(false);

  // inline edit target (mutable fields only)
  let editing = $state<CustomField | null>(null);
  let eLabel = $state("");
  let eLibraryIds = $state<string[]>([]);
  let eFacetable = $state(false);
  let eSortable = $state(false);
  let eRequired = $state(false);
  let eSelectOptions = $state("");
  let saving = $state(false);

  const libName = (id: string) => libraries.find((l) => l.id === id)?.name ?? id;

  async function refresh() {
    loading = true;
    try {
      fields = await listCustomFields();
      error = "";
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }

  onMount(refresh);

  function toggle(list: string[], id: string): string[] {
    return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
  }

  async function create(e: Event) {
    e.preventDefault();
    creating = true;
    error = "";
    try {
      await createCustomField({
        name: cName,
        label: cLabel || cName,
        data_type: cType,
        library_ids: cLibraryIds,
        facetable: cFacetable,
        sortable: cSortable,
        required: cRequired,
        select_options:
          cType === "select"
            ? cSelectOptions.split("\n").map((s) => s.trim()).filter(Boolean)
            : null,
      });
      cName = "";
      cLabel = "";
      cType = "string";
      cLibraryIds = [];
      cFacetable = cSortable = cRequired = false;
      cSelectOptions = "";
      await refresh();
    } catch (err) {
      error = String(err);
    } finally {
      creating = false;
    }
  }

  function startEdit(f: CustomField) {
    editing = f;
    eLabel = f.label;
    eLibraryIds = [...f.library_ids];
    eFacetable = f.facetable;
    eSortable = f.sortable;
    eRequired = f.required;
    eSelectOptions = (f.select_options ?? []).join("\n");
  }

  async function saveEdit() {
    if (!editing) return;
    saving = true;
    error = "";
    try {
      await updateCustomField(editing.id, {
        label: eLabel,
        library_ids: eLibraryIds,
        facetable: eFacetable,
        sortable: eSortable,
        required: eRequired,
        select_options:
          editing.data_type === "select"
            ? eSelectOptions.split("\n").map((s) => s.trim()).filter(Boolean)
            : null,
      });
      editing = null;
      await refresh();
    } catch (err) {
      error = String(err);
    } finally {
      saving = false;
    }
  }

  async function remove(f: CustomField) {
    if (
      !confirm(
        `Delete custom field "${f.label}" (${f.name})?\n\n` +
          "The definition is removed but existing values already saved under this " +
          "key on any item are kept untouched.",
      )
    )
      return;
    error = "";
    try {
      await deleteCustomField(f.id);
      await refresh();
    } catch (err) {
      error = String(err);
    }
  }
</script>

<section class="mt-8">
  <h2 class="text-lg font-semibold">Custom fields</h2>
  <p class="mt-1 max-w-2xl text-xs text-slate-500">
    Admin-defined metadata fields. Values are stored per item under
    <code>user_metadata</code> and type-checked on edit. Deleting a definition keeps
    existing values.
  </p>

  {#if error}<p class="mt-2 text-sm text-red-500">{error}</p>{/if}

  <div class="mt-3 overflow-x-auto">
    <table class="w-full min-w-[52rem] text-sm">
      <thead>
        <tr class="border-b border-slate-200 text-left text-slate-500 dark:border-slate-800">
          <th class="py-2 pr-3 font-medium">Name</th>
          <th class="py-2 pr-3 font-medium">Label</th>
          <th class="py-2 pr-3 font-medium">Type</th>
          <th class="py-2 pr-3 font-medium">Libraries</th>
          <th class="py-2 pr-3 font-medium">Facet</th>
          <th class="py-2 pr-3 font-medium">Sort</th>
          <th class="py-2 pr-3 font-medium">Req</th>
          <th class="py-2 text-right font-medium">Actions</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each fields as f (f.id)}
          <tr class="align-top">
            <td class="py-2 pr-3 font-mono text-xs">{f.name}</td>
            <td class="py-2 pr-3">{f.label}</td>
            <td class="py-2 pr-3 text-xs text-slate-500">{f.data_type}</td>
            <td class="py-2 pr-3 text-xs text-slate-500">
              {#if f.library_ids.length === 0}
                <span class="text-slate-400">all</span>
              {:else}
                {f.library_ids.map(libName).join(", ")}
              {/if}
            </td>
            <td class="py-2 pr-3">{f.facetable ? "✓" : "—"}</td>
            <td class="py-2 pr-3">{f.sortable ? "✓" : "—"}</td>
            <td class="py-2 pr-3">{f.required ? "✓" : "—"}</td>
            <td class="py-2">
              <div class="flex justify-end gap-1">
                <button
                  class="rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300"
                  onclick={() => startEdit(f)}>Edit</button>
                <button
                  class="rounded-lg border border-red-300 px-2 py-1 text-xs text-red-500 dark:border-red-800"
                  onclick={() => remove(f)}>Delete</button>
              </div>
            </td>
          </tr>
        {:else}
          <tr>
            <td colspan="8" class="py-4 text-slate-500">
              {loading ? "Loading…" : "No custom fields yet — add one below."}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  <h3 class="mt-6 text-sm font-semibold">Add custom field</h3>
  <form class="mt-3 flex max-w-xl flex-col gap-3" onsubmit={create}>
    <label class="flex items-center gap-1 text-xs text-slate-500">
      Name <Help text={HELP.cf_name} label="custom field name" />
    </label>
    <input
      class="-mt-2 rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono dark:border-slate-700"
      placeholder="e.g. shelf_location" bind:value={cName} required />

    <span class="flex items-center gap-1 text-xs text-slate-500">Label</span>
    <input
      class="-mt-2 rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
      placeholder="Human-readable label" bind:value={cLabel} />

    <label class="flex items-center gap-1 text-xs text-slate-500">
      Type <Help text={HELP.cf_data_type} label="custom field type" />
    </label>
    <select
      class="-mt-2 w-48 rounded-lg border border-slate-300 bg-transparent px-2 py-2 dark:border-slate-700"
      bind:value={cType}>
      {#each CUSTOM_FIELD_TYPES as t}<option value={t}>{t}</option>{/each}
    </select>

    {#if cType === "select"}
      <span class="flex items-center gap-1 text-xs text-slate-500">Options (one per line)</span>
      <textarea
        class="-mt-2 rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
        rows="3" bind:value={cSelectOptions}></textarea>
    {/if}

    <label class="flex items-center gap-1 text-xs text-slate-500">
      Libraries <Help text={HELP.cf_libraries} label="custom field libraries" />
    </label>
    <div class="-mt-2 flex flex-wrap gap-2">
      {#each libraries as l (l.id)}
        <button type="button"
          class="rounded-full border px-3 py-1 text-sm {cLibraryIds.includes(l.id) ? 'border-transparent bg-[var(--accent)] text-white' : 'border-slate-300 dark:border-slate-700'}"
          onclick={() => (cLibraryIds = toggle(cLibraryIds, l.id))}>{l.name}</button>
      {/each}
      <span class="self-center text-xs text-slate-500">(none = all libraries)</span>
    </div>

    <div class="flex flex-wrap items-center gap-4">
      <label class="inline-flex items-center gap-2 text-sm">
        <input type="checkbox" bind:checked={cFacetable} /> facetable
      </label>
      <label class="inline-flex items-center gap-2 text-sm">
        <input type="checkbox" bind:checked={cSortable} /> sortable
        <Help text={HELP.cf_facet_sort} label="facet/sort" />
      </label>
      <label class="inline-flex items-center gap-2 text-sm">
        <input type="checkbox" bind:checked={cRequired} /> required
        <Help text={HELP.cf_required} label="required" />
      </label>
    </div>
    {#if cFacetable || cSortable}
      <p class="-mt-1 text-xs text-amber-600">
        Faceting/sorting takes effect after an index rebuild (P4-T6).
      </p>
    {/if}

    <button
      class="self-start rounded-lg bg-[var(--accent)] px-4 py-2 text-white disabled:opacity-50"
      disabled={creating}>{creating ? "Adding…" : "Add custom field"}</button>
  </form>
</section>

{#if editing}
  <div class="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
    <div class="w-full max-w-lg rounded-xl bg-white p-5 shadow-xl dark:bg-slate-900">
      <h3 class="text-base font-semibold">
        Edit custom field <span class="font-mono text-sm text-slate-500">{editing.name}</span>
      </h3>
      <p class="mt-1 text-xs text-slate-500">
        Name (<code>{editing.name}</code>) and type (<code>{editing.data_type}</code>) are
        immutable — create a new field to change them.
      </p>

      <div class="mt-4 flex flex-col gap-3">
        <span class="text-xs text-slate-500">Label</span>
        <input
          class="-mt-2 rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
          bind:value={eLabel} />

        {#if editing.data_type === "select"}
          <span class="text-xs text-slate-500">Options (one per line)</span>
          <textarea
            class="-mt-2 rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
            rows="3" bind:value={eSelectOptions}></textarea>
        {/if}

        <label class="flex items-center gap-1 text-xs text-slate-500">
          Libraries <Help text={HELP.cf_libraries} label="custom field libraries" />
        </label>
        <div class="-mt-2 flex flex-wrap gap-2">
          {#each libraries as l (l.id)}
            <button type="button"
              class="rounded-full border px-3 py-1 text-sm {eLibraryIds.includes(l.id) ? 'border-transparent bg-[var(--accent)] text-white' : 'border-slate-300 dark:border-slate-700'}"
              onclick={() => (eLibraryIds = toggle(eLibraryIds, l.id))}>{l.name}</button>
          {/each}
          <span class="self-center text-xs text-slate-500">(none = all libraries)</span>
        </div>

        <div class="flex flex-wrap items-center gap-4">
          <label class="inline-flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={eFacetable} /> facetable
          </label>
          <label class="inline-flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={eSortable} /> sortable
          </label>
          <label class="inline-flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={eRequired} /> required
          </label>
        </div>
        {#if eFacetable || eSortable}
          <p class="-mt-1 text-xs text-amber-600">
            Faceting/sorting takes effect after an index rebuild (P4-T6).
          </p>
        {/if}
      </div>

      <div class="mt-5 flex justify-end gap-2">
        <button
          class="rounded-lg border border-slate-300 px-3 py-2 text-sm dark:border-slate-700"
          onclick={() => (editing = null)}>Cancel</button>
        <button
          class="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm text-white disabled:opacity-50"
          disabled={saving} onclick={saveEdit}>{saving ? "Saving…" : "Save"}</button>
      </div>
    </div>
  </div>
{/if}
