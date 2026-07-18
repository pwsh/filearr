<script lang="ts">
  // P3-T13 — archive contents section (shown in ItemDetail whenever the item's
  // extracted metadata carries an ``archive`` fact). Read-only: the member list
  // is index-only (names + declared sizes), never an unpack. Member NAMES are
  // untrusted strings rendered as plain text (Svelte auto-escapes; never {@html}).
  import { effectiveMeta } from "./cards/keyfacts";
  import { fmtBytes } from "./cards/format";

  let { item }: { item: Record<string, unknown> } = $props();

  type Member = { name?: unknown; size?: unknown };
  type Archive = {
    member_count?: unknown;
    total_uncompressed?: unknown;
    members?: unknown;
    truncated?: unknown;
    format?: unknown;
  };

  const meta = $derived(effectiveMeta(item));
  const archive = $derived(
    (meta.archive && typeof meta.archive === "object" ? meta.archive : null) as Archive | null,
  );
  const memberCount = $derived(
    typeof archive?.member_count === "number" ? archive.member_count : null,
  );
  const totalBytes = $derived(fmtBytes(archive?.total_uncompressed));
  const format = $derived(typeof archive?.format === "string" ? archive.format : "");
  const truncated = $derived(archive?.truncated === true);
  const members = $derived(
    Array.isArray(archive?.members) ? (archive!.members as Member[]) : [],
  );

  // Collapsed by default; a large archive shows the first N with an expander.
  const PREVIEW = 20;
  let expanded = $state(false);
  const shown = $derived(expanded ? members : members.slice(0, PREVIEW));
  const hiddenCount = $derived(Math.max(0, members.length - PREVIEW));
</script>

{#if memberCount != null}
  <div class="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
    <h3 class="mb-2 text-sm font-semibold">
      Archive contents
      <span class="ml-1 font-normal text-slate-500">
        {memberCount}
        {memberCount === 1 ? "file" : "files"}{#if format} · {format}{/if}{#if totalBytes}
          · {totalBytes} uncompressed{/if}
      </span>
    </h3>

    {#if truncated}
      <p class="mb-2 text-xs text-amber-600 dark:text-amber-400">
        Listing was capped by a size/count guard — some members are not shown.
      </p>
    {/if}

    {#if members.length}
      <ul class="flex flex-col gap-0.5 font-mono text-xs">
        {#each shown as m, i (i)}
          <li class="flex items-center gap-2">
            <span class="min-w-0 flex-1 truncate" title={String(m.name ?? "")}
              >{String(m.name ?? "")}</span>
            {#if fmtBytes(m.size)}
              <span class="shrink-0 text-slate-500">{fmtBytes(m.size)}</span>
            {/if}
          </li>
        {/each}
      </ul>
      {#if members.length > PREVIEW}
        <button
          type="button"
          class="mt-2 text-xs text-[var(--accent)]"
          onclick={() => (expanded = !expanded)}
        >
          {expanded ? "Show fewer" : `Show ${hiddenCount} more`}
        </button>
      {/if}
    {/if}
  </div>
{/if}
