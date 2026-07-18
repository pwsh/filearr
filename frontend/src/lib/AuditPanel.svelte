<script lang="ts">
  import { onMount } from "svelte";
  import { listAudit, friendlyError, type SecurityEvent } from "./api";

  // P6-T9 security audit feed (admin). Filterable by event type + time range,
  // keyset-paginated (Load more appends the next page).
  let error = $state("");
  let events = $state<SecurityEvent[]>([]);
  let nextCursor = $state<string | null>(null);
  let loading = $state(false);

  let fType = $state("");
  let fSince = $state("");
  let fUntil = $state("");

  const EVENT_TYPES = [
    "", "login_success", "ldap_login", "oidc_login", "login_failure", "lockout",
    "logout", "bootstrap", "password_change", "user_created", "user_disabled",
    "user_enabled", "role_changed", "user_deleted", "grant_created", "grant_deleted",
    "group_created", "group_deleted", "group_membership_changed", "session_revoked",
    "search",
  ];

  function isoOrEmpty(v: string): string | undefined {
    if (!v) return undefined;
    const d = new Date(v);
    return isNaN(d.getTime()) ? undefined : d.toISOString();
  }

  async function load(reset: boolean) {
    error = "";
    loading = true;
    try {
      const page = await listAudit({
        event_type: fType || undefined,
        since: isoOrEmpty(fSince),
        until: isoOrEmpty(fUntil),
        cursor: reset ? undefined : nextCursor || undefined,
        limit: 50,
      });
      events = reset ? page.events : [...events, ...page.events];
      nextCursor = page.next_cursor;
    } catch (e) {
      error = friendlyError(e);
    } finally {
      loading = false;
    }
  }

  onMount(() => load(true));

  function when(ts: string): string {
    try { return new Date(ts).toLocaleString(); } catch { return ts; }
  }
  function badgeClass(t: string): string {
    if (t === "login_failure" || t === "lockout" || t === "user_deleted")
      return "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300";
    if (t.endsWith("_login") || t === "login_success" || t === "bootstrap")
      return "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300";
    return "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300";
  }
</script>

<section class="mt-8">
  <h2 class="text-lg font-semibold">Security audit</h2>
  <p class="text-xs text-slate-500">
    Login/logout, account lifecycle, permission changes and lockouts.
  </p>

  <div class="mt-3 flex flex-wrap items-end gap-2">
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Event type</span>
      <select class="rounded border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
        bind:value={fType} onchange={() => load(true)}>
        {#each EVENT_TYPES as t}
          <option value={t}>{t === "" ? "all" : t}</option>
        {/each}
      </select>
    </label>
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Since</span>
      <input type="datetime-local" class="rounded border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
        bind:value={fSince} />
    </label>
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Until</span>
      <input type="datetime-local" class="rounded border border-slate-300 px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-900"
        bind:value={fUntil} />
    </label>
    <button class="rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
      onclick={() => load(true)}>Apply</button>
  </div>

  {#if error}
    <p class="mt-2 text-sm text-red-600">{error}</p>
  {/if}

  <div class="mt-3 overflow-x-auto">
    <table class="w-full text-sm">
      <thead class="text-left text-xs uppercase text-slate-400">
        <tr>
          <th class="py-1 pr-3">When</th>
          <th class="py-1 pr-3">Event</th>
          <th class="py-1 pr-3">Who</th>
          <th class="py-1 pr-3">IP</th>
          <th class="py-1 pr-3">Details</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each events as e (e.id)}
          <tr>
            <td class="py-1.5 pr-3 whitespace-nowrap text-xs text-slate-500">{when(e.ts)}</td>
            <td class="py-1.5 pr-3">
              <span class="rounded px-1.5 py-0.5 text-xs {badgeClass(e.event_type)}">{e.event_type}</span>
            </td>
            <td class="py-1.5 pr-3 text-xs">
              {e.username_attempted ?? (e.principal_id ? e.principal_id.slice(0, 8) : "—")}
            </td>
            <td class="py-1.5 pr-3 font-mono text-xs text-slate-500">{e.ip ?? "—"}</td>
            <td class="py-1.5 pr-3 text-xs text-slate-500">
              {e.details ? JSON.stringify(e.details) : ""}
            </td>
          </tr>
        {/each}
        {#if events.length === 0 && !loading}
          <tr><td colspan="5" class="py-3 text-slate-400">No events.</td></tr>
        {/if}
      </tbody>
    </table>
  </div>

  {#if nextCursor}
    <button class="mt-3 rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
      disabled={loading} onclick={() => load(false)}>
      {loading ? "Loading…" : "Load more"}
    </button>
  {/if}
</section>
