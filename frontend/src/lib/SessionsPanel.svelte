<script lang="ts">
  import { onMount } from "svelte";
  import {
    listMySessions, revokeMySession, revokeAllMySessions,
    listUsers, listUserSessions, revokeUserSessions,
    friendlyError, type AuthPrincipal, type AuthSession,
  } from "./api";

  // P6-T11 session management. Every signed-in user sees + revokes their OWN
  // active sessions ("log out everywhere"); an admin can additionally pick any
  // user and force-revoke all of theirs. Revocation takes effect on that
  // session's very next request (Postgres-backed instant revocation).
  let { isAdmin = false }: { isAdmin?: boolean } = $props();

  let error = $state("");
  let mine = $state<AuthSession[]>([]);

  // admin view
  let users = $state<AuthPrincipal[]>([]);
  let selected = $state("");
  let theirs = $state<AuthSession[]>([]);

  async function refreshMine() {
    error = "";
    try {
      mine = await listMySessions();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function refreshUsers() {
    try {
      users = await listUsers();
    } catch {
      users = [];
    }
  }

  async function loadTheirs() {
    theirs = [];
    if (!selected) return;
    try {
      theirs = await listUserSessions(selected);
    } catch (e) {
      error = friendlyError(e);
    }
  }

  onMount(() => {
    refreshMine();
    if (isAdmin) refreshUsers();
  });

  async function revokeOne(id: string) {
    try {
      await revokeMySession(id);
      await refreshMine();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function revokeAll() {
    if (!confirm("Sign out of ALL your sessions, including this one?")) return;
    try {
      await revokeAllMySessions();
      // This kills the current session too — reload to hit the login wall.
      location.reload();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function forceRevoke() {
    if (!selected) return;
    const u = users.find((x) => x.id === selected);
    if (!confirm(`Force sign-out ALL sessions for "${u?.username ?? selected}"?`)) return;
    try {
      await revokeUserSessions(selected);
      await loadTheirs();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  function when(ts: string): string {
    try { return new Date(ts).toLocaleString(); } catch { return ts; }
  }
  function ua(s: string | null): string {
    return s ? (s.length > 60 ? s.slice(0, 60) + "…" : s) : "unknown client";
  }
</script>

<section class="mt-8">
  <h2 class="text-lg font-semibold">Active sessions</h2>
  <p class="text-xs text-slate-500">
    Revoking a session signs it out on its next request — no waiting for expiry.
  </p>

  {#if error}
    <p class="mt-2 text-sm text-red-600">{error}</p>
  {/if}

  <ul class="mt-3 divide-y divide-slate-200 text-sm dark:divide-slate-800">
    {#each mine as s (s.id)}
      <li class="flex items-center gap-3 py-2">
        <div class="grow">
          <span class="font-mono text-xs text-slate-500">{s.ip_address ?? "—"}</span>
          <span class="ml-2">{ua(s.user_agent)}</span>
          {#if s.current}
            <span class="ml-2 rounded bg-emerald-100 px-1.5 py-0.5 text-xs text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">this session</span>
          {/if}
          <div class="text-xs text-slate-400">last seen {when(s.last_seen_at)}</div>
        </div>
        {#if !s.current}
          <button class="text-xs text-red-600 underline" onclick={() => revokeOne(s.id)}>revoke</button>
        {/if}
      </li>
    {/each}
    {#if mine.length === 0}
      <li class="py-2 text-slate-400">No active sessions.</li>
    {/if}
  </ul>

  <button class="mt-3 rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
    onclick={revokeAll}>Log out everywhere</button>

  {#if isAdmin}
    <div class="mt-6 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
      <h3 class="text-sm font-semibold">Admin: force sign-out a user</h3>
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <select class="rounded border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
          bind:value={selected} onchange={loadTheirs}>
          <option value="">Select a user…</option>
          {#each users as u (u.id)}
            <option value={u.id}>{u.username} ({u.global_role})</option>
          {/each}
        </select>
        <button class="rounded-lg border border-red-300 px-3 py-1 text-sm text-red-600 disabled:opacity-40 dark:border-red-800"
          disabled={!selected || theirs.length === 0}
          onclick={forceRevoke}>Revoke all their sessions</button>
      </div>
      {#if selected}
        <ul class="mt-2 divide-y divide-slate-200 text-sm dark:divide-slate-800">
          {#each theirs as s (s.id)}
            <li class="py-1.5">
              <span class="font-mono text-xs text-slate-500">{s.ip_address ?? "—"}</span>
              <span class="ml-2">{ua(s.user_agent)}</span>
              <span class="ml-2 text-xs text-slate-400">last seen {when(s.last_seen_at)}</span>
            </li>
          {/each}
          {#if theirs.length === 0}
            <li class="py-1.5 text-slate-400">No active sessions.</li>
          {/if}
        </ul>
      {/if}
    </div>
  {/if}
</section>
