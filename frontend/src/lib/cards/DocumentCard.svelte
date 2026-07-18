<script lang="ts">
  import Highlights from "./Highlights.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { asStr } from "./format";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  const CURATED = [
    "pages", "author", "producer", "creator", "subject", "keywords",
    "encrypted", "paragraphs", "revision", "created", "modified",
    "title", "unsupported", "body_text",
  ];

  const highlights = $derived([
    { label: "Pages", value: asStr(m.pages) },
    { label: "Author", value: asStr(m.author) },
    { label: "Created", value: asStr(m.created) },
    { label: "Modified", value: asStr(m.modified) },
    { label: "Encrypted", value: m.encrypted == null ? null : m.encrypted ? "yes" : "no" },
    { label: "Paragraphs", value: asStr(m.paragraphs) },
  ]);
  // P4-T10: note whether extracted body text is present (searchable via P3-T5).
  const hasBody = $derived(typeof m.body_text === "string" && (m.body_text as string).trim().length > 0);
</script>

<div class="space-y-4">
  <Highlights facts={highlights} />
  <p class="text-xs text-slate-500">
    {#if hasBody}Body text extracted and searchable.{:else}No extractable body text.{/if}
  </p>
  <KeyFactsCard {item} exclude={CURATED} heading="More details" />
</div>
