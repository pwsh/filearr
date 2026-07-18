<script lang="ts">
  // UI-T2 (frontend) — typed-name confirmation for the product's ONE intentional
  // hard-delete. The user must type the exact library name; the delete button
  // stays disabled until it matches. Calls DELETE /libraries/{id}?confirm=<name>.
  // 409 (scan running) and other statuses are classified into a clear message.
  import { deleteLibrary, type Library } from "./api";

  let {
    library,
    onDeleted,
    onClose,
  }: { library: Library; onDeleted: () => void; onClose: () => void } = $props();

  let typed = $state("");
  let error = $state("");
  let busy = $state(false);

  const matches = $derived(typed === library.name);

  async function confirmDelete() {
    if (!matches || busy) return;
    busy = true;
    error = "";
    try {
      await deleteLibrary(library.id, typed);
      onDeleted();
    } catch (e) {
      const msg = String(e);
      // The frozen contract: 409 = a scan is running; 422 = confirm mismatch
      // (shouldn't happen given the client-side gate, but surfaced anyway).
      if (msg.startsWith("Error: 409") || msg.includes("409:")) {
        error =
          "A scan is currently running for this library. Cancel or wait for it " +
          "to finish, then delete.";
      } else if (msg.includes("422:")) {
        error = "Confirmation name did not match. Type the library name exactly.";
      } else {
        error = msg;
      }
    } finally {
      busy = false;
    }
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") onClose();
  }
</script>

<svelte:window onkeydown={onKey} />

<div class="fixed inset-0 z-[70] overflow-y-auto">
  <button
    type="button"
    class="absolute inset-0 h-full w-full cursor-default bg-black/50"
    aria-label="Cancel delete"
    onclick={onClose}
  ></button>

  <div
    class="relative z-10 mx-auto mt-32 mb-8 w-full max-w-md rounded-2xl bg-white p-5 shadow-xl dark:bg-slate-900"
    role="dialog"
    aria-modal="true"
    aria-label="Delete library"
  >
    <h3 class="text-base font-semibold text-red-600 dark:text-red-400">Delete library</h3>
    <p class="mt-2 text-sm text-slate-600 dark:text-slate-300">
      This permanently removes <span class="font-semibold">{library.name}</span>, all of its
      indexed items, and its scan history. Search results for these items disappear. This is a
      hard delete and cannot be undone.
    </p>
    <p class="mt-3 text-sm text-slate-600 dark:text-slate-300">
      Type the library name <span class="font-mono font-semibold">{library.name}</span> to confirm:
    </p>
    <input
      class="mt-2 w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm outline-none
             focus:border-red-500 dark:border-slate-700"
      placeholder="library name"
      autocomplete="off"
      bind:value={typed}
    />

    {#if error}<p class="mt-3 text-sm text-red-500">{error}</p>{/if}

    <div class="mt-5 flex justify-end gap-2">
      <button
        class="rounded-lg border border-slate-300 px-4 py-2 text-sm dark:border-slate-700"
        onclick={onClose}>Cancel</button>
      <button
        class="rounded-lg bg-red-600 px-4 py-2 text-sm text-white disabled:opacity-40"
        disabled={!matches || busy}
        onclick={confirmDelete}>
        {busy ? "Deleting…" : "Delete permanently"}
      </button>
    </div>
  </div>
</div>
