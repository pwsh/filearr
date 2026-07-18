<script lang="ts">
  // P10-T10 — hosting-agent identity, online status, and inline Verify for an
  // agent-hosted item. Shows the owning agent's name, an online/offline badge
  // (with a relative "last seen"), the "last verified …" freshness, and a split
  // Verify button (fast stat / full rehash). Issuing a Verify polls
  // ``/agent-status`` until ``last_verified_at`` advances (freshness updates in
  // place — NO full transfer) or a timeout, which then messages clearly (offline
  // agents queue the check; never a bare spinner). Mirrors RetrievePanel's tone.
  import {
    friendlyError,
    itemAgentStatus,
    verifyItem,
    type ItemAgentStatus,
  } from "./api";

  let { itemId }: { itemId: string } = $props();

  let status = $state<ItemAgentStatus | null>(null);
  let verifying = $state(false);
  let message = $state<string | null>(null);

  // Bumped whenever the item changes; async loops capture it and bail if stale.
  let token = 0;

  const online = $derived(status?.online === true);
  const agentStatus = $derived(status?.agent_status ?? "active");
  const inFlight = $derived(status?.verify_in_flight === true);
  const lastVerifiedAt = $derived(status?.last_verified_at ?? null);
  const lastSeenAt = $derived(status?.last_seen_at ?? null);

  function relTime(iso: string | null | undefined): string {
    if (!iso) return "";
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  }

  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

  async function refresh(): Promise<ItemAgentStatus | null> {
    const mine = token;
    try {
      const s = await itemAgentStatus(itemId);
      if (mine !== token) return null; // a newer item superseded this fetch
      status = s;
      return s;
    } catch {
      return null; // a transient failure just leaves the last-known status
    }
  }

  // Poll agent-status until last_verified_at moves past ``baseline`` (the check
  // completed) or the budget lapses. Offline / lapsed → a clear queued message.
  async function pollUntilVerified(baseline: string | null): Promise<void> {
    const mine = token;
    const deadline = Date.now() + 60_000; // ~1 min budget, then message + stop
    while (Date.now() < deadline) {
      await sleep(3000);
      if (mine !== token) return;
      const s = await refresh();
      if (mine !== token) return;
      if (s && s.agent_hosted && (s.last_verified_at ?? null) !== baseline) {
        message = `Verified ${relTime(s.last_verified_at)}`;
        return;
      }
    }
    if (mine !== token) return;
    message = online
      ? "Still verifying — the agent hasn't reported back yet. It will update shortly."
      : "Agent offline — the check is queued and will run once it reconnects.";
  }

  async function runVerify(mode: "stat" | "rehash") {
    if (verifying || inFlight) return;
    verifying = true;
    const baseline = lastVerifiedAt;
    message = mode === "stat" ? "Verification requested…" : "Full rehash requested…";
    try {
      await verifyItem(itemId, mode);
      await refresh(); // reflect verify_in_flight immediately
      await pollUntilVerified(baseline);
    } catch (e) {
      message = friendlyError(e);
    } finally {
      verifying = false;
    }
  }

  $effect(() => {
    itemId; // track
    token += 1;
    status = null;
    verifying = false;
    message = null;
    void refresh();
  });
</script>

{#if status && status.agent_hosted}
  <div
    class="flex flex-wrap items-center gap-2 border-b border-slate-200 pb-3 text-sm dark:border-slate-800"
  >
    <span class="font-medium text-slate-600 dark:text-slate-300">Agent-hosted</span>
    {#if status.agent_name}
      <span class="font-mono text-slate-500">{status.agent_name}</span>
    {/if}

    <!-- Online / offline badge. -->
    {#if online}
      <span
        class="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
      >
        <span class="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>online
      </span>
    {:else}
      <span
        class="inline-flex items-center gap-1 rounded-full bg-slate-200 px-2 py-0.5 text-xs font-medium text-slate-500 dark:bg-slate-800 dark:text-slate-400"
        title={lastSeenAt ?? "never seen"}
      >
        <span class="h-1.5 w-1.5 rounded-full bg-slate-400"></span>
        offline{#if lastSeenAt} · seen {relTime(lastSeenAt)}{/if}
      </span>
    {/if}

    <!-- Lifecycle chip for a non-active agent. -->
    {#if agentStatus === "revoked"}
      <span class="rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700 dark:bg-red-900/40 dark:text-red-300">revoked</span>
    {:else if agentStatus === "pending"}
      <span class="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">pending</span>
    {/if}

    <!-- Freshness. -->
    {#if lastVerifiedAt}
      <span class="text-slate-500" title={lastVerifiedAt}>last verified {relTime(lastVerifiedAt)}</span>
    {:else}
      <span class="text-slate-400">never verified</span>
    {/if}

    <span class="grow"></span>
    <div class="inline-flex overflow-hidden rounded-lg border border-slate-300 dark:border-slate-700">
      <button
        type="button"
        class="px-3 py-1 disabled:opacity-50"
        disabled={verifying || inFlight || agentStatus === "revoked"}
        onclick={() => runVerify("stat")}
        title="Ask the agent to confirm the file still exists (fast)">Verify</button>
      <button
        type="button"
        class="border-l border-slate-300 px-3 py-1 disabled:opacity-50 dark:border-slate-700"
        disabled={verifying || inFlight || agentStatus === "revoked"}
        onclick={() => runVerify("rehash")}
        title="Ask the agent to re-hash the file's contents (slower, detects silent changes)"
        >Full rehash</button>
    </div>
  </div>
  {#if message}
    <p class="mt-2 text-xs text-[var(--accent)]" role="status">{message}</p>
  {/if}
{/if}
