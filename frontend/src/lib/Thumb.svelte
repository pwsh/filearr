<script lang="ts">
  // S12/P12 slice 1: a small square thumbnail with graceful fallback. On any
  // load error (skipped media type, thumbnail not generated yet, auth off) the
  // <img> hides itself, revealing the media-type badge/icon underneath — one
  // client-side convention covering both "never will have one" and "not yet".
  import { thumbUrl } from "./api";

  interface Props {
    id: string;
    tier?: "grid" | "preview";
    size?: string; // tailwind size classes, e.g. "h-10 w-10"
    rounded?: string;
  }
  let { id, tier = "grid", size = "h-10 w-10", rounded = "rounded" }: Props = $props();
  let failed = $state(false);
</script>

{#if !failed}
  <img
    src={thumbUrl(id, tier)}
    alt=""
    loading="lazy"
    class="{size} {rounded} shrink-0 bg-slate-100 object-cover dark:bg-slate-800"
    onerror={() => (failed = true)}
  />
{/if}
