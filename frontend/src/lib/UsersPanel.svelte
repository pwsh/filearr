<script lang="ts">
  import { onMount } from "svelte";
  import {
    listUsers, createUser, updateUser, deleteUser,
    friendlyError, type AuthPrincipal,
  } from "./api";

  // P6-T12 user management (admin). Lists local + federated accounts with a
  // provider + kind badge, a role selector, a disable toggle, password reset and
  // delete. Federated (ldap/oidc/saml) accounts have no local password, so the
  // reset action is hidden for them.
  let error = $state("");
  let users = $state<AuthPrincipal[]>([]);

  // create form
  let nuName = $state("");
  let nuPass = $state("");
  let nuRole = $state<"admin" | "user" | "viewer">("user");
  let nuEmail = $state("");

  // password reset
  let resetFor = $state<string | null>(null);
  let resetPass = $state("");

  async function refresh() {
    error = "";
    try {
      users = await listUsers();
    } catch (e) {
      error = friendlyError(e);
    }
  }
  onMount(refresh);

  async function addUser(e: Event) {
    e.preventDefault();
    error = "";
    try {
      await createUser({
        username: nuName.trim(),
        password: nuPass,
        global_role: nuRole,
        email: nuEmail.trim() || null,
      });
      nuName = ""; nuPass = ""; nuEmail = ""; nuRole = "user";
      await refresh();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function setRole(u: AuthPrincipal, role: "admin" | "user" | "viewer") {
    error = "";
    try {
      await updateUser(u.id, { global_role: role });
      await refresh();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function toggleDisabled(u: AuthPrincipal) {
    error = "";
    try {
      await updateUser(u.id, { disabled: !u.disabled });
      await refresh();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function doReset(u: AuthPrincipal) {
    error = "";
    try {
      await updateUser(u.id, { password: resetPass });
      resetFor = null; resetPass = "";
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function remove(u: AuthPrincipal) {
    if (!confirm(`Delete user "${u.username}"? This cannot be undone.`)) return;
    error = "";
    try {
      await deleteUser(u.id);
      await refresh();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  function providerBadge(p: string | undefined): string {
    return p && p !== "local" ? p.toUpperCase() : "local";
  }
</script>

<section class="mt-8">
  <h2 class="text-lg font-semibold">Users</h2>
  <p class="text-xs text-slate-500">
    Local and federated accounts. Roles: admin (full) · user (read+write) · viewer
    (read). Federated accounts sign in through their identity provider.
  </p>

  {#if error}
    <p class="mt-2 text-sm text-red-600">{error}</p>
  {/if}

  <div class="mt-3 overflow-x-auto">
    <table class="w-full text-sm">
      <thead class="text-left text-xs uppercase text-slate-400">
        <tr>
          <th class="py-1 pr-3">User</th>
          <th class="py-1 pr-3">Source</th>
          <th class="py-1 pr-3">Role</th>
          <th class="py-1 pr-3">Status</th>
          <th class="py-1 pr-3"></th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each users as u (u.id)}
          <tr>
            <td class="py-2 pr-3">
              <span class="font-medium">{u.username}</span>
              {#if u.email}<span class="ml-1 text-xs text-slate-400">{u.email}</span>{/if}
            </td>
            <td class="py-2 pr-3">
              <span class="rounded bg-slate-100 px-1.5 py-0.5 text-xs dark:bg-slate-800">
                {providerBadge(u.auth_provider)}
              </span>
              {#if u.kind && u.kind !== "user"}
                <span class="ml-1 rounded bg-indigo-100 px-1.5 py-0.5 text-xs text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
                  {u.kind}
                </span>
              {/if}
            </td>
            <td class="py-2 pr-3">
              <select
                class="rounded border border-slate-300 bg-transparent px-1 py-0.5 text-sm dark:border-slate-700"
                value={u.global_role}
                onchange={(e) => setRole(u, (e.currentTarget as HTMLSelectElement).value as any)}>
                <option value="admin">admin</option>
                <option value="user">user</option>
                <option value="viewer">viewer</option>
              </select>
            </td>
            <td class="py-2 pr-3">
              {#if u.disabled}
                <span class="text-amber-600">disabled</span>
              {:else}
                <span class="text-emerald-600">active</span>
              {/if}
            </td>
            <td class="py-2 pr-3 text-right">
              <button class="text-xs text-slate-500 underline" onclick={() => toggleDisabled(u)}>
                {u.disabled ? "enable" : "disable"}
              </button>
              {#if !u.auth_provider || u.auth_provider === "local"}
                <button class="ml-2 text-xs text-slate-500 underline"
                  onclick={() => { resetFor = resetFor === u.id ? null : u.id; resetPass = ""; }}>
                  reset password
                </button>
              {/if}
              <button class="ml-2 text-xs text-red-600 underline" onclick={() => remove(u)}>
                delete
              </button>
              {#if resetFor === u.id}
                <div class="mt-1 flex items-center gap-2">
                  <input
                    class="rounded border border-slate-300 px-2 py-0.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                    type="password" placeholder="new password (min 8)"
                    bind:value={resetPass} />
                  <button class="rounded bg-[var(--accent)] px-2 py-0.5 text-xs text-white"
                    disabled={resetPass.length < 8}
                    onclick={() => doReset(u)}>save</button>
                </div>
              {/if}
            </td>
          </tr>
        {/each}
        {#if users.length === 0}
          <tr><td colspan="5" class="py-3 text-slate-400">No users yet.</td></tr>
        {/if}
      </tbody>
    </table>
  </div>

  <form onsubmit={addUser} class="mt-4 flex flex-wrap items-end gap-2">
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Username</span>
      <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
        bind:value={nuName} required />
    </label>
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Password (min 8)</span>
      <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
        type="password" bind:value={nuPass} minlength="8" required />
    </label>
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Email (optional)</span>
      <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
        bind:value={nuEmail} />
    </label>
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Role</span>
      <select class="rounded border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-700"
        bind:value={nuRole}>
        <option value="admin">admin</option>
        <option value="user">user</option>
        <option value="viewer">viewer</option>
      </select>
    </label>
    <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white"
      disabled={nuName.trim().length === 0 || nuPass.length < 8}>Add user</button>
  </form>
</section>
