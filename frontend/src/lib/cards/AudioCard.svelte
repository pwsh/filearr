<script lang="ts">
  // Covers both `audio` and `audiobook` (audiobook adds chapters).
  import Highlights from "./Highlights.svelte";
  import TrackTable from "./TrackTable.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { asStr, asRows, fmtDuration } from "./format";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  const CURATED = [
    "artist", "album", "genre", "year", "duration", "bitrate", "samplerate",
    "channels", "title", "chapters", "chapter_count",
  ];

  const highlights = $derived([
    { label: "Artist", value: asStr(m.artist) },
    { label: "Album", value: asStr(m.album) },
    { label: "Genre", value: asStr(m.genre) },
    { label: "Year", value: asStr(m.year) },
    { label: "Duration", value: fmtDuration(m.duration) },
    { label: "Bitrate", value: m.bitrate ? `${asStr(m.bitrate)} kbps` : null },
    { label: "Sample rate", value: m.samplerate ? `${asStr(m.samplerate)} Hz` : null },
    { label: "Channels", value: asStr(m.channels) },
  ]);
  const chapters = $derived(asRows(m.chapters));
</script>

<div class="space-y-4">
  <Highlights facts={highlights} />
  {#if chapters}<TrackTable title="Chapters" rows={chapters} />{/if}
  <KeyFactsCard {item} exclude={CURATED} heading="More details" />
</div>
