<script lang="ts">
  // P4-T12 — custom-field-aware key-facts. The generic fallback card AND the
  // "more details" section every per-type card embeds. Orders/labels fields by
  // profile (P4-T1) -> applicable custom fields (P4-T3) -> ad-hoc keys
  // (alphabetical), rendering text-only values (see keyfacts.ts).
  import { getMetadataProfile, listCustomFields } from "../api";
  import {
    effectiveMeta,
    orderKeyFacts,
    profileFieldLabels,
    customFieldLabels,
    type FieldLabel,
  } from "./keyfacts";

  let {
    item,
    exclude = [],
    heading = "",
  }: { item: Record<string, unknown>; exclude?: string[]; heading?: string } = $props();

  const mediaType = $derived(typeof item.file_category === "string" ? item.file_category : "");
  const libraryId = $derived(typeof item.library_id === "string" ? item.library_id : "");
  const meta = $derived(effectiveMeta(item));

  let profileLabels = $state<FieldLabel[]>([]);
  let cfLabels = $state<FieldLabel[]>([]);

  // Profile labels/order (best-effort: an unknown/unseeded type -> no profile,
  // fields then fall back to their raw key names).
  $effect(() => {
    const mt = mediaType;
    if (!mt) {
      profileLabels = [];
      return;
    }
    getMetadataProfile(mt)
      .then((p) => (profileLabels = profileFieldLabels(p.fields)))
      .catch(() => (profileLabels = []));
  });

  // Applicable custom fields, narrowed by media_type + library (empty
  // applies_to/library_ids = "all"). Best-effort; a fetch failure -> none.
  $effect(() => {
    const mt = mediaType;
    const lib = libraryId;
    listCustomFields()
      .then((defs) => {
        const applicable = defs.filter(
          (d) =>
            (!d.applies_to?.length || d.applies_to.includes(mt)) &&
            (!d.library_ids?.length || d.library_ids.includes(lib)),
        );
        cfLabels = customFieldLabels(applicable);
      })
      .catch(() => (cfLabels = []));
  });

  const facts = $derived(orderKeyFacts(meta, profileLabels, cfLabels, { exclude }));
</script>

{#if facts.length}
  <div>
    {#if heading}
      <div class="mb-1 text-xs font-semibold text-slate-500">{heading}</div>
    {/if}
    <dl class="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {#each facts as f (f.key)}
        <dt class="text-slate-500">{f.label}</dt>
        <dd class="min-w-0 break-words">{f.value}</dd>
      {/each}
    </dl>
  </div>
{:else if !heading}
  <p class="text-sm text-slate-500">No additional details extracted.</p>
{/if}
