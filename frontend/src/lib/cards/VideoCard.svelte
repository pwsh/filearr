<script lang="ts">
  import Highlights from "./Highlights.svelte";
  import TrackTable from "./TrackTable.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { asStr, asRows, fmtDuration } from "./format";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  const CURATED = [
    "resolution", "video_codec", "audio_codec", "duration", "container",
    "hdr", "hdr_format", "width", "height", "frame_rate", "bitrate",
    "color_primaries", "color_transfer", "audio_tracks", "subtitle_tracks",
    "title", "year", "season", "episode",
  ];

  const highlights = $derived([
    { label: "Resolution", value: asStr(m.resolution) ?? (m.width && m.height ? `${m.width}×${m.height}` : null) },
    { label: "Video codec", value: asStr(m.video_codec) },
    { label: "Audio codec", value: asStr(m.audio_codec) },
    { label: "Duration", value: fmtDuration(m.duration) },
    { label: "Frame rate", value: m.frame_rate ? `${asStr(m.frame_rate)} fps` : null },
    { label: "HDR", value: m.hdr ? (asStr(m.hdr_format) ?? "yes") : null },
    { label: "Container", value: asStr(m.container) },
  ]);
  const audioTracks = $derived(asRows(m.audio_tracks));
  const subtitleTracks = $derived(asRows(m.subtitle_tracks));
</script>

<div class="space-y-4">
  <Highlights facts={highlights} />
  {#if audioTracks}<TrackTable title="Audio tracks" rows={audioTracks} />{/if}
  {#if subtitleTracks}<TrackTable title="Subtitle tracks" rows={subtitleTracks} />{/if}
  <KeyFactsCard {item} exclude={CURATED} heading="More details" />
</div>
