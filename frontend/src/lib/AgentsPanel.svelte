<script lang="ts">
  import { onMount } from "svelte";
  import { copyText } from "./clipboard";
  import {
    listAgents, listEnrollmentTokens, mintEnrollmentToken, revokeAgent, deleteAgent,
    revokeEnrollmentToken, listAgentCommands, cancelAgentCommand,
    AGENT_COMMAND_TERMINAL,
    listAgentShareMaps, createAgentShareMap, deleteAgentShareMap,
    type AgentOut, type EnrollmentTokenOut, type AgentCommandOut,
    type AgentCommandStatus, type AgentShareMapOut,
  } from "./api";

  // P5-T1 distributed-agent enrollment admin panel: mint single-use enrollment
  // tokens (show-once + copy), list/revoke tokens, list/revoke agents. Only
  // rendered when the server reports agents_enabled (opt-in v3 feature).
  let error = $state("");
  let agents = $state<AgentOut[]>([]);
  let tokens = $state<EnrollmentTokenOut[]>([]);

  // mint form + show-once result
  let newGroup = $state("default");
  let newTtl = $state<number | undefined>(undefined);
  let minting = $state(false);
  let minted = $state<{ token: string; expires_at: string } | null>(null);
  let copied = $state(false);

  async function refresh() {
    error = "";
    try {
      [agents, tokens] = await Promise.all([listAgents(), listEnrollmentTokens()]);
    } catch (e) {
      error = String(e);
    }
  }
  onMount(refresh);

  async function mint() {
    if (!newGroup.trim()) return;
    minting = true;
    copied = false;
    try {
      const r = await mintEnrollmentToken(newGroup.trim(), newTtl || undefined);
      minted = { token: r.token, expires_at: r.expires_at };
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      minting = false;
    }
  }

  async function copyToken() {
    if (!minted) return;
    copied = await copyText(minted.token);
  }

  async function dropToken(hash: string, force = false) {
    if (force && !confirm("Delete this consumed token row? Its consumed-by link is preserved in the audit log.")) return;
    try {
      await revokeEnrollmentToken(hash, force);
      await refresh();
    } catch (e) { error = String(e); }
  }

  async function dropAgent(id: string, name: string) {
    if (!confirm(`Revoke agent "${name}"? It will be denied all replication/config access.`)) return;
    try {
      await revokeAgent(id);
      await refresh();
    } catch (e) { error = String(e); }
  }

  // Hard delete: the cleanup path for failed enrollments (pending rows) and
  // decommissioned machines with no replicated data. The server refuses (409)
  // while any library/item still references the agent — that error surfaces here.
  async function purgeAgent(id: string, name: string) {
    if (!confirm(`DELETE agent "${name}" permanently? Only possible while it owns no libraries/items; use Revoke for data-owning agents.`)) return;
    try {
      await deleteAgent(id);
      await refresh();
    } catch (e) { error = String(e); }
  }

  // P10-T1: per-agent commands drawer (list + cancel a pre-terminal command).
  let openCommands = $state<string | null>(null);
  let commands = $state<AgentCommandOut[]>([]);
  let cmdError = $state("");

  async function toggleCommands(agentId: string) {
    if (openCommands === agentId) { openCommands = null; return; }
    openCommands = agentId;
    commands = [];
    cmdError = "";
    try {
      commands = await listAgentCommands(agentId);
    } catch (e) { cmdError = String(e); }
  }

  async function dropCommand(id: string) {
    try {
      await cancelAgentCommand(id);
      if (openCommands) commands = await listAgentCommands(openCommands);
    } catch (e) { cmdError = String(e); }
  }

  function cmdChip(s: AgentCommandStatus): string {
    if (s === "done") return "text-green-600";
    if (s === "failed" || s === "expired") return "text-red-600";
    if (s === "picked_up") return "text-blue-600";
    if (s === "cancelled") return "text-slate-400";
    return "text-amber-600"; // pending
  }
  const isTerminal = (s: AgentCommandStatus) => AGENT_COMMAND_TERMINAL.includes(s);

  // P10-T12: per-agent share-maps drawer (list/add/delete a central fallback
  // mapping of a local path prefix -> a network share for agent-hosted files).
  let openShareMaps = $state<string | null>(null);
  let shareMaps = $state<AgentShareMapOut[]>([]);
  let smError = $state("");
  let smLocal = $state("");
  let smShare = $state("");
  let smBusy = $state(false);

  async function toggleShareMaps(agentId: string) {
    if (openShareMaps === agentId) { openShareMaps = null; return; }
    openShareMaps = agentId;
    shareMaps = [];
    smError = "";
    smLocal = "";
    smShare = "";
    try {
      shareMaps = await listAgentShareMaps(agentId);
    } catch (e) { smError = String(e); }
  }

  async function addShareMap(agentId: string) {
    if (!smLocal.trim() || !smShare.trim()) return;
    smBusy = true;
    smError = "";
    try {
      await createAgentShareMap(agentId, {
        local_prefix: smLocal.trim(),
        share_prefix: smShare.trim(),
      });
      smLocal = "";
      smShare = "";
      shareMaps = await listAgentShareMaps(agentId);
    } catch (e) { smError = String(e); }
    finally { smBusy = false; }
  }

  async function dropShareMap(id: string) {
    try {
      await deleteAgentShareMap(id);
      if (openShareMaps) shareMaps = await listAgentShareMaps(openShareMaps);
    } catch (e) { smError = String(e); }
  }

  function fmt(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : "—";
  }
  function statusClass(s: string): string {
    if (s === "active") return "text-green-600";
    if (s === "revoked") return "text-red-600";
    return "text-amber-600"; // pending / consumed / expired
  }
</script>

<section class="mt-8">
  <h2 class="text-lg font-semibold">Agents (distributed fleet)</h2>
  <p class="mt-1 text-xs text-slate-500">
    Mint a single-use, short-lived enrollment token, hand it to a new machine's
    agent installer out-of-band, and the agent registers itself here. Tokens are
    shown once and never stored in the clear.
  </p>

  {#if error}<p class="mt-2 text-sm text-red-600">{error}</p>{/if}

  <!-- mint form -->
  <div class="mt-3 flex flex-wrap items-end gap-2">
    <label class="text-xs text-slate-500">
      rollout group
      <input class="mt-1 block rounded border px-2 py-1 text-sm" bind:value={newGroup} />
    </label>
    <label class="text-xs text-slate-500">
      TTL (min, blank = default)
      <input class="mt-1 block w-40 rounded border px-2 py-1 text-sm" type="number" min="1"
        placeholder="60" bind:value={newTtl} />
    </label>
    <button class="rounded bg-[var(--accent)] px-3 py-1 text-sm text-white"
      disabled={minting} onclick={mint}>Mint token</button>
  </div>

  {#if minted}
    <div class="mt-2 rounded border border-amber-400 bg-amber-50 p-2 text-sm dark:bg-amber-950/30">
      <p class="font-medium">Copy this token now — it will not be shown again.</p>
      <div class="mt-1 flex items-center gap-2">
        <code class="grow break-all font-mono text-xs">{minted.token}</code>
        <button class="rounded border px-2 py-1 text-xs" onclick={copyToken}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p class="mt-1 text-xs text-slate-500">Expires {fmt(minted.expires_at)}</p>
    </div>
  {/if}

  <!-- tokens -->
  <h3 class="mt-4 font-medium">Enrollment tokens</h3>
  {#if tokens.length === 0}
    <p class="py-2 text-slate-400">No tokens.</p>
  {:else}
    <table class="mt-1 w-full text-sm">
      <thead class="text-left text-slate-500">
        <tr><th class="py-1">hash</th><th>group</th><th>status</th><th>expires</th><th></th></tr>
      </thead>
      <tbody>
        {#each tokens as t (t.token_hash)}
          <tr class="border-t">
            <td class="py-1 font-mono text-xs">{t.token_hash.slice(0, 12)}…</td>
            <td>{t.rollout_group}</td>
            <td class={statusClass(t.status)}>{t.status}</td>
            <td class="text-slate-500">{fmt(t.expires_at)}</td>
            <td class="text-right">
              {#if t.status === "active"}
                <button class="text-red-600" onclick={() => dropToken(t.token_hash)}>revoke</button>
              {:else}
                <button class="text-red-600" onclick={() => dropToken(t.token_hash, true)}>delete</button>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <!-- agents -->
  <h3 class="mt-6 font-medium">Registered agents</h3>
  {#if agents.length === 0}
    <p class="py-2 text-slate-400">No agents registered.</p>
  {:else}
    <table class="mt-1 w-full text-sm">
      <thead class="text-left text-slate-500">
        <tr>
          <th class="py-1">name</th><th>host</th><th>platform</th><th>group</th>
          <th>status</th><th>version</th><th>last seen</th><th></th>
        </tr>
      </thead>
      <tbody>
        {#each agents as a (a.id)}
          <tr class="border-t">
            <td class="py-1 font-medium">{a.name}</td>
            <td class="font-mono text-xs">{a.hostname}</td>
            <td>{a.platform}</td>
            <td>{a.rollout_group}</td>
            <td class={statusClass(a.status)}>{a.status}</td>
            <td class="text-slate-500">{a.agent_version ?? "—"}</td>
            <td class="text-slate-500">{fmt(a.last_seen_at)}</td>
            <td class="text-right whitespace-nowrap">
              <button class="text-[var(--accent)]" onclick={() => toggleCommands(a.id)}>
                {openCommands === a.id ? "hide" : "commands"}
              </button>
              <button class="ml-3 text-[var(--accent)]" onclick={() => toggleShareMaps(a.id)}>
                {openShareMaps === a.id ? "hide shares" : "shares"}
              </button>
              {#if a.status !== "revoked"}
                <button class="ml-3 text-red-600" onclick={() => dropAgent(a.id, a.name)}>revoke</button>
              {/if}
              <button class="ml-3 text-red-600" title="Hard delete — only while the agent owns no libraries/items"
                onclick={() => purgeAgent(a.id, a.name)}>delete</button>
            </td>
          </tr>
          {#if openCommands === a.id}
            <tr class="border-t bg-slate-50 dark:bg-slate-900/40">
              <td colspan="8" class="p-2">
                {#if cmdError}<p class="text-sm text-red-600">{cmdError}</p>{/if}
                {#if commands.length === 0}
                  <p class="text-xs text-slate-400">No commands queued for this agent.</p>
                {:else}
                  <table class="w-full text-xs">
                    <thead class="text-left text-slate-500">
                      <tr><th class="py-1">kind</th><th>status</th><th>attempts</th><th>created</th><th>expires</th><th></th></tr>
                    </thead>
                    <tbody>
                      {#each commands as cmd (cmd.id)}
                        <tr class="border-t border-slate-200 dark:border-slate-700">
                          <td class="py-1 font-mono">{cmd.kind}</td>
                          <td class={cmdChip(cmd.status)}>{cmd.status}</td>
                          <td class="text-slate-500">{cmd.attempts}</td>
                          <td class="text-slate-500">{fmt(cmd.created_at)}</td>
                          <td class="text-slate-500">{fmt(cmd.expires_at)}</td>
                          <td class="text-right">
                            {#if !isTerminal(cmd.status)}
                              <button class="text-red-600" onclick={() => dropCommand(cmd.id)}>cancel</button>
                            {/if}
                          </td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                {/if}
              </td>
            </tr>
          {/if}
          {#if openShareMaps === a.id}
            <tr class="border-t bg-slate-50 dark:bg-slate-900/40">
              <td colspan="8" class="p-2">
                <p class="text-xs text-slate-500">
                  Central share-maps: when this agent can't self-report a network
                  share, map a local path prefix to a share so its files still get
                  a network-open link. Longest prefix wins.
                </p>
                {#if smError}<p class="mt-1 text-sm text-red-600">{smError}</p>{/if}
                {#if shareMaps.length === 0}
                  <p class="mt-1 text-xs text-slate-400">No share-maps for this agent.</p>
                {:else}
                  <table class="mt-1 w-full text-xs">
                    <thead class="text-left text-slate-500">
                      <tr><th class="py-1">local prefix</th><th>share</th><th>UNC</th><th></th></tr>
                    </thead>
                    <tbody>
                      {#each shareMaps as sm (sm.id)}
                        <tr class="border-t border-slate-200 dark:border-slate-700">
                          <td class="py-1 font-mono break-all">{sm.local_prefix}</td>
                          <td class="font-mono break-all">{sm.share_prefix}</td>
                          <td class="font-mono break-all text-slate-500">{sm.location.unc ?? "—"}</td>
                          <td class="text-right">
                            <button class="text-red-600" onclick={() => dropShareMap(sm.id)}>delete</button>
                          </td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                {/if}
                <div class="mt-2 flex flex-wrap items-end gap-2">
                  <label class="text-xs text-slate-500">
                    local prefix
                    <input class="mt-1 block rounded border px-2 py-1 text-xs"
                      placeholder="/data/media or C:\media" bind:value={smLocal} />
                  </label>
                  <label class="text-xs text-slate-500">
                    share prefix
                    <input class="mt-1 block rounded border px-2 py-1 text-xs"
                      placeholder="smb://nas/media or \\nas\media" bind:value={smShare} />
                  </label>
                  <button class="rounded bg-[var(--accent)] px-3 py-1 text-xs text-white"
                    disabled={smBusy} onclick={() => addShareMap(a.id)}>add</button>
                </div>
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
  {/if}
</section>
