<script lang="ts">
  import Highlights from "./Highlights.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { asStr } from "./format";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  const CURATED = [
    "triangles", "vertices", "mesh_count", "bbox", "bbox_volume",
    "watertight", "file_format", "unsupported",
  ];

  const highlights = $derived([
    { label: "Triangles", value: asStr(m.triangles) },
    { label: "Vertices", value: asStr(m.vertices) },
    { label: "Meshes", value: asStr(m.mesh_count) },
    { label: "Format", value: asStr(m.file_format) },
    { label: "Watertight", value: m.watertight == null ? null : m.watertight ? "yes" : "no" },
    { label: "BBox volume", value: asStr(m.bbox_volume) },
    { label: "Bounding box", value: Array.isArray(m.bbox) ? (m.bbox as unknown[]).join(" × ") : null },
  ]);
</script>

<div class="space-y-4">
  {#if m.unsupported}
    <p class="rounded-lg bg-amber-100 px-3 py-2 text-sm text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
      Unsupported / non-mesh geometry — stats may be incomplete.
    </p>
  {/if}
  <Highlights facts={highlights} />
  <KeyFactsCard {item} exclude={CURATED} heading="More details" />
</div>
