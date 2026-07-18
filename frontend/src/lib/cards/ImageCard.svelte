<script lang="ts">
  import Highlights from "./Highlights.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { asStr } from "./format";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  const CURATED = ["width", "height", "format", "mode", "camera", "taken_at"];

  const highlights = $derived([
    { label: "Dimensions", value: m.width && m.height ? `${asStr(m.width)}×${asStr(m.height)}` : null },
    { label: "Format", value: asStr(m.format) },
    { label: "Colour mode", value: asStr(m.mode) },
    { label: "Camera", value: asStr(m.camera) },
    { label: "Taken at", value: asStr(m.taken_at) },
  ]);
</script>

<div class="space-y-4">
  <Highlights facts={highlights} />
  <KeyFactsCard {item} exclude={CURATED} heading="More details" />
</div>
