<script lang="ts">
  import { onMount } from "svelte";
  import FolderPicker from "./FolderPicker.svelte";
  import {
    addMember, createGrant, createGroup, deleteGrant, deleteGroup, listGrants,
    listGroups, listMembers, listRbacActions, listUsers, listLibraries, rbacPreview,
    removeMember,
    type AuthPrincipal, type Library, type RbacActions, type RbacDecision,
    type RbacGrant, type RbacGroup, type RbacMember,
  } from "./api";

  // P6-T2 RBAC admin panel: groups + members, path grants, and a decision
  // preview. Functional, not fancy. Enforcement on data endpoints is P6-T4.
  let error = $state("");
  let users = $state<AuthPrincipal[]>([]);
  let groups = $state<RbacGroup[]>([]);
  let grants = $state<RbacGrant[]>([]);
  let libraries = $state<Library[]>([]);
  let meta = $state<RbacActions>({ actions: [], role_ceilings: {} });

  // group create + member management
  let newGroupName = $state("");
  let newGroupDesc = $state("");
  let openGroup = $state<string | null>(null);
  let members = $state<RbacMember[]>([]);
  let addMemberId = $state("");

  // grant create form
  let gSubjectKind = $state<"principal" | "group">("group");
  let gSubjectId = $state("");
  let gLibraryId = $state("");
  let gRelPath = $state("");
  let gAction = $state("search_metadata");
  let gEffect = $state<"allow" | "deny">("allow");
  let showPicker = $state(false);

  // preview form
  let pPrincipal = $state("");
  let pLibrary = $state("");
  let pPath = $state("");
  let pAction = $state("search_metadata");
  let decision = $state<RbacDecision | null>(null);

  async function refresh() {
    error = "";
    try {
      [users, groups, grants, libraries, meta] = await Promise.all([
        listUsers().catch(() => []),
        listGroups(),
        listGrants(),
        listLibraries(),
        listRbacActions(),
      ]);
      if (meta.actions.length && !meta.actions.includes(gAction)) gAction = meta.actions[0];
    } catch (e) {
      error = String(e);
    }
  }
  onMount(refresh);

  async function addGroup() {
    if (!newGroupName.trim()) return;
    try {
      await createGroup(newGroupName.trim(), newGroupDesc.trim() || null);
      newGroupName = "";
      newGroupDesc = "";
      await refresh();
    } catch (e) { error = String(e); }
  }
  async function removeGroup(id: string) {
    if (!confirm("Delete this group and its grants?")) return;
    try { await deleteGroup(id); if (openGroup === id) openGroup = null; await refresh(); }
    catch (e) { error = String(e); }
  }
  async function toggleMembers(id: string) {
    if (openGroup === id) { openGroup = null; return; }
    openGroup = id;
    try { members = await listMembers(id); } catch (e) { error = String(e); }
  }
  async function doAddMember() {
    if (!openGroup || !addMemberId) return;
    try { await addMember(openGroup, addMemberId); members = await listMembers(openGroup); await refresh(); }
    catch (e) { error = String(e); }
  }
  async function doRemoveMember(pid: string) {
    if (!openGroup) return;
    try { await removeMember(openGroup, pid); members = await listMembers(openGroup); await refresh(); }
    catch (e) { error = String(e); }
  }

  function onPickPath(abs: string) {
    showPicker = false;
    const lib = libraries.find((l) => l.id === gLibraryId);
    if (lib && abs.startsWith(lib.root_path)) {
      gRelPath = abs.slice(lib.root_path.length).replace(/^\/+/, "");
    } else {
      gRelPath = abs.replace(/^\/+/, "");
    }
  }

  async function addGrant() {
    if (!gSubjectId || !gLibraryId) { error = "Pick a subject and a library"; return; }
    try {
      await createGrant({
        subject_kind: gSubjectKind, subject_id: gSubjectId, library_id: gLibraryId,
        rel_path: gRelPath.trim(), action: gAction, effect: gEffect,
      });
      gRelPath = "";
      await refresh();
    } catch (e) { error = String(e); }
  }
  async function removeGrant(id: string) {
    try { await deleteGrant(id); await refresh(); } catch (e) { error = String(e); }
  }

  async function runPreview() {
    if (!pPrincipal || !pLibrary) { error = "Pick a user and a library"; return; }
    try { decision = await rbacPreview(pPrincipal, pLibrary, pPath, pAction); }
    catch (e) { error = String(e); decision = null; }
  }

  const libName = (id: string) => libraries.find((l) => l.id === id)?.name ?? id;
</script>

<section class="mt-8">
  <h2 class="text-lg font-semibold">Access control (RBAC)</h2>
  <p class="text-sm text-slate-500">
    Groups + path-scoped grants. Grants only ever narrow a principal within its
    global-role ceiling. Enforcement on data endpoints ships next (P6-T4); this
    panel builds, stores, and previews decisions.
  </p>
  {#if error}<p class="mt-2 text-sm text-red-600">{error}</p>{/if}

  <!-- Groups -->
  <h3 class="mt-4 font-medium">Groups</h3>
  <div class="mt-2 flex flex-wrap gap-2">
    <input class="rounded border px-2 py-1 text-sm" placeholder="new group name"
      bind:value={newGroupName} />
    <input class="rounded border px-2 py-1 text-sm" placeholder="description (optional)"
      bind:value={newGroupDesc} />
    <button class="rounded bg-[var(--accent)] px-3 py-1 text-sm text-white" onclick={addGroup}>
      Add group
    </button>
  </div>
  <table class="mt-2 w-full text-sm">
    <thead><tr class="text-left text-slate-500">
      <th class="py-1">Name</th><th>Source</th><th>Members</th><th></th>
    </tr></thead>
    <tbody>
      {#each groups as g (g.id)}
        <tr class="border-t">
          <td class="py-1">{g.name}{#if g.description}<span class="ml-1 text-slate-400">— {g.description}</span>{/if}</td>
          <td>{g.source}</td>
          <td>{g.member_count}</td>
          <td class="text-right">
            <button class="text-[var(--accent)]" onclick={() => toggleMembers(g.id)}>
              {openGroup === g.id ? "hide" : "members"}
            </button>
            {#if g.source === "local"}
              <button class="ml-2 text-red-600" onclick={() => removeGroup(g.id)}>delete</button>
            {/if}
          </td>
        </tr>
        {#if openGroup === g.id}
          <tr class="bg-slate-50"><td colspan="4" class="p-2">
            <div class="flex flex-wrap items-center gap-2">
              <select class="rounded border px-2 py-1 text-sm" bind:value={addMemberId}>
                <option value="">add member…</option>
                {#each users as u (u.id)}<option value={u.id}>{u.username} ({u.global_role})</option>{/each}
              </select>
              <button class="rounded border px-2 py-1 text-sm" onclick={doAddMember}>Add</button>
            </div>
            <ul class="mt-2">
              {#each members as m (m.principal_id)}
                <li class="flex items-center gap-2">
                  <span>{m.username ?? m.principal_id} <span class="text-slate-400">({m.global_role})</span></span>
                  <button class="text-red-600" onclick={() => doRemoveMember(m.principal_id)}>remove</button>
                </li>
              {:else}<li class="text-slate-400">no members</li>{/each}
            </ul>
          </td></tr>
        {/if}
      {:else}<tr><td colspan="4" class="py-2 text-slate-400">no groups yet</td></tr>{/each}
    </tbody>
  </table>

  <!-- Grants -->
  <h3 class="mt-6 font-medium">Path grants</h3>
  <div class="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3 lg:grid-cols-6">
    <select class="rounded border px-2 py-1 text-sm" bind:value={gSubjectKind}>
      <option value="group">group</option>
      <option value="principal">user</option>
    </select>
    <select class="rounded border px-2 py-1 text-sm" bind:value={gSubjectId}>
      <option value="">subject…</option>
      {#if gSubjectKind === "group"}
        {#each groups as g (g.id)}<option value={g.id}>{g.name}</option>{/each}
      {:else}
        {#each users as u (u.id)}<option value={u.id}>{u.username}</option>{/each}
      {/if}
    </select>
    <select class="rounded border px-2 py-1 text-sm" bind:value={gLibraryId}>
      <option value="">library…</option>
      {#each libraries as l (l.id)}<option value={l.id}>{l.name}</option>{/each}
    </select>
    <input class="rounded border px-2 py-1 text-sm" placeholder="rel path (blank = whole library)"
      bind:value={gRelPath} />
    <select class="rounded border px-2 py-1 text-sm" bind:value={gAction}>
      {#each meta.actions as a}<option value={a}>{a}</option>{/each}
    </select>
    <div class="flex gap-2">
      <select class="rounded border px-2 py-1 text-sm" bind:value={gEffect}>
        <option value="allow">allow</option>
        <option value="deny">deny</option>
      </select>
      <button class="rounded bg-[var(--accent)] px-3 py-1 text-sm text-white" onclick={addGrant}>Add</button>
    </div>
  </div>
  <button class="mt-1 text-xs text-[var(--accent)]"
    onclick={() => { if (!gLibraryId) { error = "Pick a library first"; } else { showPicker = true; } }}>
    browse folder…
  </button>
  {#if showPicker}
    {@const lib = libraries.find((l) => l.id === gLibraryId)}
    <FolderPicker initial={lib?.root_path ?? ""} onPick={onPickPath} onClose={() => (showPicker = false)} />
  {/if}
  <table class="mt-2 w-full text-sm">
    <thead><tr class="text-left text-slate-500">
      <th class="py-1">Subject</th><th>Library</th><th>Scope</th><th>Action</th><th>Effect</th><th></th>
    </tr></thead>
    <tbody>
      {#each grants as gr (gr.id)}
        <tr class="border-t">
          <td class="py-1">{gr.subject_label ?? gr.subject_id} <span class="text-slate-400">({gr.subject_kind})</span></td>
          <td>{libName(gr.library_id)}</td>
          <td class="font-mono text-xs">{gr.scope}</td>
          <td>{gr.action}</td>
          <td class={gr.effect === "deny" ? "text-red-600" : "text-green-700"}>{gr.effect}</td>
          <td class="text-right"><button class="text-red-600" onclick={() => removeGrant(gr.id)}>delete</button></td>
        </tr>
      {:else}<tr><td colspan="6" class="py-2 text-slate-400">no grants yet</td></tr>{/each}
    </tbody>
  </table>

  <!-- Preview -->
  <h3 class="mt-6 font-medium">Decision preview</h3>
  <div class="mt-2 flex flex-wrap gap-2">
    <select class="rounded border px-2 py-1 text-sm" bind:value={pPrincipal}>
      <option value="">user…</option>
      {#each users as u (u.id)}<option value={u.id}>{u.username} ({u.global_role})</option>{/each}
    </select>
    <select class="rounded border px-2 py-1 text-sm" bind:value={pLibrary}>
      <option value="">library…</option>
      {#each libraries as l (l.id)}<option value={l.id}>{l.name}</option>{/each}
    </select>
    <input class="rounded border px-2 py-1 text-sm" placeholder="rel path" bind:value={pPath} />
    <select class="rounded border px-2 py-1 text-sm" bind:value={pAction}>
      {#each meta.actions as a}<option value={a}>{a}</option>{/each}
    </select>
    <button class="rounded border px-3 py-1 text-sm" onclick={runPreview}>Evaluate</button>
  </div>
  {#if decision}
    <div class="mt-2 rounded border p-2 text-sm">
      <span class={decision.allowed ? "font-semibold text-green-700" : "font-semibold text-red-600"}>
        {decision.allowed ? "ALLOWED" : "DENIED"}
      </span>
      <span class="ml-2 text-slate-500">reason: {decision.reason} · role: {decision.role}</span>
      <div class="mt-1 font-mono text-xs text-slate-500">scope: {decision.item_scope}</div>
      {#if decision.winning_grant}
        <div class="mt-1 text-xs">
          winning grant: {decision.winning_grant.effect} {decision.winning_grant.action}
          @ <span class="font-mono">{decision.winning_grant.scope}</span>
          ({decision.winning_grant.subject_kind})
        </div>
      {/if}
    </div>
  {/if}
</section>
