<script lang="ts">
  // P8-T12/T13 — the Alerts admin surface: notification channels, alert rules
  // (file-watch + read-only is_system operational rules) and a recent-events
  // view with failed-delivery surfacing. All admin-scope. Secret channel fields
  // are password inputs whose decrypted value NEVER round-trips (empty on edit =
  // keep, via the "__unchanged__" sentinel). Every dynamic string renders as
  // text (Svelte auto-escapes) — a crafted path/error can't inject markup.
  import { onMount } from "svelte";
  import {
    CHANNEL_TYPES,
    DISPATCH_LOCALITIES,
    DIGEST_WINDOWS,
    EVENT_TYPES,
    UNCHANGED,
    alertEventsSummary,
    createAlertChannel,
    createAlertRule,
    deleteAlertChannel,
    deleteAlertRule,
    listAlertChannels,
    listAlertEvents,
    listAlertRules,
    listLibraries,
    testAlertChannel,
    updateAlertChannel,
    updateAlertRule,
    WEBHOOK_FORMATS,
    detectWebhookFormat,
    type AlertChannel,
    type AlertEvent,
    type AlertEventStatus,
    type AlertEventSummary,
    type AlertRule,
    type ChannelType,
    type DispatchLocality,
    type DigestWindow,
    type WebhookFormat,
    type Library,
    type TestFireResult,
  } from "./api";
  import { HELP } from "./help";
  import Help from "./Help.svelte";

  type Tab = "channels" | "rules" | "events";
  let tab = $state<Tab>("channels");

  let libraries = $state<Library[]>([]);
  let channels = $state<AlertChannel[]>([]);
  let rules = $state<AlertRule[]>([]);
  let error = $state("");
  let loading = $state(true);

  const libName = (id: string | null) =>
    id ? (libraries.find((l) => l.id === id)?.name ?? id) : "All libraries";
  const chName = (id: string) => channels.find((c) => c.id === id)?.name ?? id;

  async function refresh() {
    loading = true;
    try {
      [libraries, channels, rules] = await Promise.all([
        listLibraries(),
        listAlertChannels(),
        listAlertRules(),
      ]);
      error = "";
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }
  onMount(refresh);

  // ---- channels -------------------------------------------------------- //
  let chEditing = $state<AlertChannel | null>(null); // null while not editing
  let chNew = $state(false);
  let chName_ = $state("");
  let chType = $state<ChannelType>("webhook");
  let chLocality = $state<DispatchLocality>("central");
  let chEnabled = $state(true);
  // per-type config fields
  let cWebhookUrl = $state("");
  let cWebhookSecret = $state("");
  let cWebhookFormat = $state<WebhookFormat>("generic");
  // FIX-16: once the operator picks a format by hand, stop auto-detecting from URL.
  let chFormatTouched = $state(false);
  let cEmailHost = $state("");
  let cEmailPort = $state(587);
  let cEmailSecurity = $state("starttls");
  let cEmailFrom = $state("");
  let cEmailTo = $state("");
  let cEmailUser = $state("");
  let cEmailPassword = $state("");
  let cAppriseUrl = $state("");
  let chBusy = $state(false);
  let testResults = $state<Record<string, TestFireResult | { error: string }>>({});

  function resetChannelForm() {
    chEditing = null;
    chNew = false;
    chName_ = "";
    chType = "webhook";
    chLocality = "central";
    chEnabled = true;
    cWebhookUrl = cWebhookSecret = "";
    cWebhookFormat = "generic";
    chFormatTouched = false;
    cEmailHost = cEmailFrom = cEmailTo = cEmailUser = cEmailPassword = "";
    cEmailPort = 587;
    cEmailSecurity = "starttls";
    cAppriseUrl = "";
  }

  function startNewChannel() {
    resetChannelForm();
    chNew = true;
  }

  function startEditChannel(c: AlertChannel) {
    resetChannelForm();
    chEditing = c;
    chName_ = c.name;
    chType = c.type;
    chLocality = c.dispatch_locality;
    chEnabled = c.enabled;
    const cfg = c.config ?? {};
    if (c.type === "webhook") {
      cWebhookUrl = String(cfg.url ?? "");
      cWebhookSecret = ""; // secret redacted on read; empty = keep
      cWebhookFormat = (String(cfg.webhook_format ?? "generic") as WebhookFormat);
      chFormatTouched = true; // an existing channel keeps its stored format
    } else if (c.type === "email") {
      cEmailHost = String(cfg.host ?? "");
      cEmailPort = Number(cfg.port ?? 587);
      cEmailSecurity = String(cfg.security ?? "starttls");
      cEmailFrom = String(cfg.from_addr ?? cfg.from ?? "");
      const to = cfg.to ?? cfg.to_addrs ?? [];
      cEmailTo = Array.isArray(to) ? to.join(", ") : String(to);
      cEmailUser = String(cfg.username ?? "");
      cEmailPassword = ""; // keep unless typed
    } else {
      cAppriseUrl = ""; // whole url is the secret; empty = keep
    }
  }

  // Build the config for the API. `editing` decides whether an empty secret
  // means "keep" (send the UNCHANGED sentinel) rather than "clear".
  function buildConfig(editing: boolean): Record<string, unknown> {
    if (chType === "webhook") {
      const secret = cWebhookSecret
        ? cWebhookSecret
        : editing
          ? UNCHANGED
          : undefined;
      return {
        url: cWebhookUrl,
        webhook_format: cWebhookFormat,
        ...(secret !== undefined ? { secret } : {}),
      };
    }
    if (chType === "email") {
      const password = cEmailPassword
        ? cEmailPassword
        : editing
          ? UNCHANGED
          : undefined;
      return {
        host: cEmailHost,
        port: cEmailPort,
        security: cEmailSecurity,
        from_addr: cEmailFrom,
        to: cEmailTo.split(/[\n,]/).map((s) => s.trim()).filter(Boolean),
        username: cEmailUser,
        ...(password !== undefined ? { password } : {}),
      };
    }
    // apprise: the whole url is the secret
    const url = cAppriseUrl ? cAppriseUrl : editing ? UNCHANGED : "";
    return { url };
  }

  // FIX-16: auto-detect the format from the URL until the operator overrides it.
  function onWebhookUrlInput() {
    if (!chFormatTouched) cWebhookFormat = detectWebhookFormat(cWebhookUrl);
  }

  async function saveChannel(e: Event) {
    e.preventDefault();
    chBusy = true;
    error = "";
    try {
      if (chEditing) {
        await updateAlertChannel(chEditing.id, {
          name: chName_,
          config: buildConfig(true),
          dispatch_locality: chLocality,
          enabled: chEnabled,
        });
      } else {
        await createAlertChannel({
          name: chName_,
          type: chType,
          config: buildConfig(false),
          dispatch_locality: chLocality,
          enabled: chEnabled,
        });
      }
      resetChannelForm();
      await refresh();
    } catch (err) {
      error = String(err);
    } finally {
      chBusy = false;
    }
  }

  async function removeChannel(c: AlertChannel) {
    if (!confirm(`Delete channel "${c.name}"? Rules using it lose this destination.`)) return;
    try {
      await deleteAlertChannel(c.id);
      await refresh();
    } catch (err) {
      error = String(err);
    }
  }

  async function toggleChannelEnabled(c: AlertChannel) {
    try {
      await updateAlertChannel(c.id, { enabled: !c.enabled });
      await refresh();
    } catch (err) {
      error = String(err);
    }
  }

  async function fireTest(c: AlertChannel) {
    testResults = { ...testResults, [c.id]: { error: "…" } as unknown as TestFireResult };
    try {
      testResults = { ...testResults, [c.id]: await testAlertChannel(c.id) };
    } catch (err) {
      testResults = { ...testResults, [c.id]: { error: String(err) } };
    }
  }

  // ---- rules ----------------------------------------------------------- //
  let rlEditing = $state<AlertRule | null>(null);
  let rlNew = $state(false);
  let rName = $state("");
  let rLibrary = $state<string>(""); // "" = all libraries
  let rGlob = $state("");
  let rEvents = $state<string[]>(["created"]);
  let rHashOnly = $state(false);
  let rThrottle = $state<"immediate" | "digest">("immediate");
  let rGroupWait = $state(30);
  let rDigest = $state<DigestWindow>("hourly");
  let rRepeat = $state<string>(""); // seconds, blank = none
  let rChannels = $state<string[]>([]);
  let rEnabled = $state(true);
  let rShowAdvanced = $state(false);
  let rlBusy = $state(false);

  const isSystem = $derived(rlEditing?.is_system ?? false);

  function resetRuleForm() {
    rlEditing = null;
    rlNew = false;
    rName = "";
    rLibrary = "";
    rGlob = "";
    rEvents = ["created"];
    rHashOnly = false;
    rThrottle = "immediate";
    rGroupWait = 30;
    rDigest = "hourly";
    rRepeat = "";
    rChannels = [];
    rEnabled = true;
    rShowAdvanced = false;
  }

  function startNewRule() {
    resetRuleForm();
    rlNew = true;
  }

  function startEditRule(r: AlertRule) {
    resetRuleForm();
    rlEditing = r;
    rName = r.name;
    rLibrary = r.library_id ?? "";
    rGlob = r.path_glob ?? "";
    rEvents = [...r.event_types];
    rHashOnly = r.hash_change_only;
    rThrottle = r.digest_window ? "digest" : "immediate";
    rGroupWait = r.group_wait_s;
    rDigest = r.digest_window ?? "hourly";
    rRepeat = r.repeat_interval_s != null ? String(r.repeat_interval_s) : "";
    rChannels = [...r.channel_ids];
    rEnabled = r.enabled;
  }

  function toggle(list: string[], id: string): string[] {
    return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
  }

  async function saveRule(e: Event) {
    e.preventDefault();
    rlBusy = true;
    error = "";
    try {
      const repeat = rRepeat.trim() === "" ? null : Number(rRepeat);
      if (rlEditing) {
        // System rules: only channels + throttle/timings are editable.
        const patch: Record<string, unknown> = {
          enabled: rEnabled,
          group_wait_s: rThrottle === "immediate" ? rGroupWait : 0,
          digest_window: rThrottle === "digest" ? rDigest : null,
          repeat_interval_s: repeat,
          channel_ids: rChannels,
        };
        if (!rlEditing.is_system) {
          patch.name = rName;
          patch.library_id = rLibrary || null;
          patch.path_glob = rGlob || null;
          patch.event_types = rEvents;
          patch.hash_change_only = rHashOnly && rEvents.includes("modified");
        }
        await updateAlertRule(rlEditing.id, patch);
      } else {
        await createAlertRule({
          name: rName,
          enabled: rEnabled,
          library_id: rLibrary || null,
          path_glob: rGlob || null,
          event_types: rEvents,
          hash_change_only: rHashOnly && rEvents.includes("modified"),
          group_wait_s: rThrottle === "immediate" ? rGroupWait : 0,
          digest_window: rThrottle === "digest" ? rDigest : null,
          repeat_interval_s: repeat,
          channel_ids: rChannels,
        });
      }
      resetRuleForm();
      await refresh();
    } catch (err) {
      error = String(err);
    } finally {
      rlBusy = false;
    }
  }

  async function removeRule(r: AlertRule) {
    if (!confirm(`Delete rule "${r.name}"?`)) return;
    try {
      await deleteAlertRule(r.id);
      await refresh();
    } catch (err) {
      error = String(err);
    }
  }

  async function toggleRuleEnabled(r: AlertRule) {
    try {
      await updateAlertRule(r.id, { enabled: !r.enabled });
      await refresh();
    } catch (err) {
      error = String(err);
    }
  }

  function throttleLabel(r: AlertRule): string {
    if (r.digest_window) return `${r.digest_window} digest`;
    return `immediate (group ${r.group_wait_s}s)`;
  }

  // ---- events ---------------------------------------------------------- //
  let events = $state<AlertEvent[]>([]);
  let summary = $state<AlertEventSummary | null>(null);
  let evRule = $state<string>("");
  let evStatus = $state<AlertEventStatus | "">("");
  let evLoading = $state(false);
  let expanded = $state<Record<string, boolean>>({});

  async function loadEvents() {
    evLoading = true;
    try {
      [events, summary] = await Promise.all([
        listAlertEvents({
          rule_id: evRule || undefined,
          status: evStatus || undefined,
          limit: 100,
        }),
        alertEventsSummary(),
      ]);
      error = "";
    } catch (e) {
      error = String(e);
    } finally {
      evLoading = false;
    }
  }

  $effect(() => {
    if (tab === "events") {
      // re-run when filters change
      void evRule;
      void evStatus;
      loadEvents();
    }
  });

  const ruleName = (id: string) => rules.find((r) => r.id === id)?.name ?? id.slice(0, 8);
  const fmt = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");
</script>

<div class="space-y-4">
  <div class="flex items-center gap-2">
    <h2 class="text-lg font-semibold">Alerts</h2>
    <div class="grow"></div>
    <nav class="flex gap-1 text-sm">
      {#each [["channels", "Channels"], ["rules", "Rules"], ["events", "Events"]] as [id, label] (id)}
        <button
          class="rounded-lg px-3 py-1 {tab === id ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
          onclick={() => (tab = id as Tab)}>{label}</button>
      {/each}
    </nav>
  </div>

  {#if error}
    <div class="rounded-lg bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950 dark:text-red-300">
      {error}
    </div>
  {/if}

  {#if summary && summary.failed > 0}
    <button
      class="w-full rounded-lg bg-amber-50 p-3 text-left text-sm text-amber-800 dark:bg-amber-950 dark:text-amber-200"
      onclick={() => {
        tab = "events";
        evStatus = "failed";
      }}>
      ⚠ {summary.failed} alert{summary.failed === 1 ? "" : "s"} failed to deliver (retries exhausted).
      Click to review.
    </button>
  {/if}

  {#if loading}
    <p class="text-sm text-slate-500">Loading…</p>
  {:else if tab === "channels"}
    <!-- ============================ CHANNELS ============================ -->
    <div class="flex items-center justify-between">
      <p class="text-sm text-slate-500">Notification destinations.</p>
      <button class="rounded-lg border px-3 py-1 text-sm dark:border-slate-700" onclick={startNewChannel}>
        + New channel
      </button>
    </div>

    {#if channels.length === 0}
      <p class="text-sm text-slate-500">No channels yet.</p>
    {:else}
      <table class="w-full text-sm">
        <thead class="text-left text-slate-500">
          <tr><th class="py-1">Name</th><th>Type</th><th>Locality</th><th>Enabled</th><th></th></tr>
        </thead>
        <tbody>
          {#each channels as c (c.id)}
            <tr class="border-t border-slate-100 dark:border-slate-800">
              <td class="py-1 font-medium">{c.name}</td>
              <td>{c.type}</td>
              <td>{c.dispatch_locality}</td>
              <td>
                <button class="underline" onclick={() => toggleChannelEnabled(c)}>
                  {c.enabled ? "on" : "off"}
                </button>
              </td>
              <td class="space-x-2 text-right">
                <button class="underline" onclick={() => fireTest(c)}>test</button>
                <button class="underline" onclick={() => startEditChannel(c)}>edit</button>
                <button class="text-red-600 underline" onclick={() => removeChannel(c)}>delete</button>
              </td>
            </tr>
            {#if testResults[c.id]}
              <tr>
                <td colspan="5" class="pb-2 text-xs">
                  {#if "error" in testResults[c.id]}
                    <span class="text-red-600">{(testResults[c.id] as { error: string }).error}</span>
                  {:else}
                    {@const r = testResults[c.id] as TestFireResult}
                    <span class={r.ok ? "text-green-600" : "text-red-600"}>
                      {r.ok ? "delivered ✓" : `failed: ${r.detail}`}
                      {#if r.status_code}(HTTP {r.status_code}){/if}
                    </span>
                  {/if}
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    {/if}

    {#if chNew || chEditing}
      <form class="space-y-3 rounded-lg border border-slate-200 p-4 dark:border-slate-800" onsubmit={saveChannel}>
        <h3 class="font-medium">{chEditing ? "Edit channel" : "New channel"}</h3>
        <div class="grid gap-3 sm:grid-cols-2">
          <label class="block text-sm">
            <span class="text-slate-500">Name</span>
            <input class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={chName_} required />
          </label>
          <label class="block text-sm">
            <span class="text-slate-500">Type</span>
            <select
              class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
              bind:value={chType}
              disabled={!!chEditing}>
              {#each CHANNEL_TYPES as t (t)}<option value={t}>{t}</option>{/each}
            </select>
          </label>
        </div>

        {#if chType === "webhook"}
          <label class="block text-sm">
            <span class="text-slate-500">URL</span>
            <input class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={cWebhookUrl} oninput={onWebhookUrlInput} placeholder="https://…" required />
          </label>
          <label class="block text-sm">
            <span class="text-slate-500">Payload format <Help text={HELP.webhook_format} /></span>
            <select
              class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
              bind:value={cWebhookFormat}
              onchange={() => (chFormatTouched = true)}>
              {#each WEBHOOK_FORMATS as f (f)}<option value={f}>{f}</option>{/each}
            </select>
            <span class="mt-1 block text-xs text-slate-400">
              Auto-detected from the URL. <b>generic</b> = Filearr signed JSON;
              <b>discord</b> / <b>slack</b> reshape the body for those endpoints
              (and skip the HMAC signature).
            </span>
          </label>
          <label class="block text-sm">
            <span class="text-slate-500">HMAC secret {chEditing ? "(leave blank to keep)" : "(optional)"}</span>
            <input type="password" autocomplete="new-password" class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={cWebhookSecret} />
            {#if cWebhookFormat !== "generic"}
              <span class="mt-1 block text-xs text-slate-400">Not sent for {cWebhookFormat} — signature is only added to the generic format.</span>
            {/if}
          </label>
        {:else if chType === "email"}
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="block text-sm"><span class="text-slate-500">SMTP host</span>
              <input class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={cEmailHost} required /></label>
            <label class="block text-sm"><span class="text-slate-500">Port</span>
              <input type="number" class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={cEmailPort} /></label>
            <label class="block text-sm"><span class="text-slate-500">Security</span>
              <select class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={cEmailSecurity}>
                <option value="starttls">STARTTLS</option><option value="ssl">SSL</option><option value="plain">plain (insecure)</option>
              </select></label>
            <label class="block text-sm"><span class="text-slate-500">From</span>
              <input class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={cEmailFrom} required /></label>
            <label class="block text-sm sm:col-span-2"><span class="text-slate-500">To (comma-separated)</span>
              <input class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={cEmailTo} required /></label>
            <label class="block text-sm"><span class="text-slate-500">Username</span>
              <input class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={cEmailUser} /></label>
            <label class="block text-sm"><span class="text-slate-500">Password {chEditing ? "(blank = keep)" : ""}</span>
              <input type="password" autocomplete="new-password" class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={cEmailPassword} /></label>
          </div>
        {:else}
          <label class="block text-sm">
            <span class="text-slate-500">Apprise URL {chEditing ? "(blank = keep)" : ""} — the whole URL is stored encrypted</span>
            <input type="password" autocomplete="new-password" class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={cAppriseUrl} placeholder="discord://… / tgram://… / ntfy://…" />
          </label>
        {/if}

        <div class="grid gap-3 sm:grid-cols-2">
          <label class="block text-sm"><span class="text-slate-500">Dispatch locality</span>
            <select class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={chLocality}>
              {#each DISPATCH_LOCALITIES as l (l)}<option value={l}>{l}</option>{/each}
            </select></label>
          <label class="mt-6 flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={chEnabled} /> Enabled
          </label>
        </div>

        <div class="flex gap-2">
          <button type="submit" disabled={chBusy} class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white disabled:opacity-50">
            {chBusy ? "Saving…" : "Save"}
          </button>
          <button type="button" class="rounded-lg border px-3 py-1 text-sm dark:border-slate-700" onclick={resetChannelForm}>Cancel</button>
        </div>
      </form>
    {/if}
  {:else if tab === "rules"}
    <!-- ============================= RULES ============================== -->
    <div class="flex items-center justify-between">
      <p class="text-sm text-slate-500">File-watch rules and built-in system rules.</p>
      <button class="rounded-lg border px-3 py-1 text-sm dark:border-slate-700" onclick={startNewRule}>+ New rule</button>
    </div>

    <table class="w-full text-sm">
      <thead class="text-left text-slate-500">
        <tr><th class="py-1">Name</th><th>Scope</th><th>Events</th><th>Throttle</th><th>Channels</th><th>Enabled</th><th></th></tr>
      </thead>
      <tbody>
        {#each rules as r (r.id)}
          <tr class="border-t border-slate-100 dark:border-slate-800">
            <td class="py-1 font-medium">
              {r.name}
              {#if r.is_system}<span class="ml-1 rounded bg-slate-200 px-1 text-xs text-slate-600 dark:bg-slate-700 dark:text-slate-300">system</span>{/if}
            </td>
            <td>{libName(r.library_id)}</td>
            <td class="text-xs">{r.event_types.join(", ")}</td>
            <td class="text-xs">{throttleLabel(r)}</td>
            <td>{r.channel_ids.length}</td>
            <td><button class="underline" onclick={() => toggleRuleEnabled(r)}>{r.enabled ? "on" : "off"}</button></td>
            <td class="space-x-2 text-right">
              <button class="underline" onclick={() => startEditRule(r)}>edit</button>
              {#if !r.is_system}<button class="text-red-600 underline" onclick={() => removeRule(r)}>delete</button>{/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>

    {#if rlNew || rlEditing}
      <form class="space-y-3 rounded-lg border border-slate-200 p-4 dark:border-slate-800" onsubmit={saveRule}>
        <h3 class="font-medium">
          {rlEditing ? (isSystem ? "Edit system rule" : "Edit rule") : "New rule"}
        </h3>
        {#if isSystem}
          <p class="text-xs text-slate-500">
            System rule: its match logic is fixed. You can attach channels and tune the throttle/timings only.
          </p>
        {/if}

        <label class="block text-sm">
          <span class="text-slate-500">Name</span>
          <input class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900 disabled:opacity-60" bind:value={rName} disabled={isSystem} required />
        </label>

        {#if !isSystem}
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="block text-sm">
              <span class="text-slate-500">Library scope</span>
              <select class="mt-1 w-full rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={rLibrary}>
                <option value="">All libraries</option>
                {#each libraries as l (l.id)}<option value={l.id}>{l.name}</option>{/each}
              </select>
            </label>
            <label class="block text-sm">
              <span class="text-slate-500">Path glob <Help text={HELP.exclude_globs} /></span>
              <input class="mt-1 w-full rounded border px-2 py-1 font-mono dark:border-slate-700 dark:bg-slate-900" bind:value={rGlob} placeholder="**/*.mkv (blank = all files)" />
            </label>
          </div>

          <fieldset class="text-sm">
            <span class="text-slate-500">Event types</span>
            <div class="mt-1 flex flex-wrap gap-3">
              {#each EVENT_TYPES as et (et)}
                <label class="flex items-center gap-1">
                  <input type="checkbox" checked={rEvents.includes(et)} onchange={() => (rEvents = toggle(rEvents, et))} />
                  {et}
                </label>
              {/each}
            </div>
          </fieldset>

          <label class="flex items-center gap-2 text-sm" class:opacity-50={!rEvents.includes("modified")}>
            <input type="checkbox" bind:checked={rHashOnly} disabled={!rEvents.includes("modified")} />
            Only when the content hash changes (modified events)
          </label>
        {/if}

        <fieldset class="text-sm">
          <span class="text-slate-500">Throttle</span>
          <div class="mt-1 space-y-2">
            <label class="flex items-center gap-2">
              <input type="radio" value="immediate" bind:group={rThrottle} />
              Immediate — group matches for
              <input type="number" min="0" class="w-20 rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={rGroupWait} disabled={rThrottle !== "immediate"} />
              seconds
            </label>
            <label class="flex items-center gap-2">
              <input type="radio" value="digest" bind:group={rThrottle} />
              Digest —
              <select class="rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={rDigest} disabled={rThrottle !== "digest"}>
                {#each DIGEST_WINDOWS as w (w)}<option value={w}>{w}</option>{/each}
              </select>
              roll-up
            </label>
          </div>
        </fieldset>

        <button type="button" class="text-xs text-slate-500 underline" onclick={() => (rShowAdvanced = !rShowAdvanced)}>
          {rShowAdvanced ? "Hide" : "Show"} advanced
        </button>
        {#if rShowAdvanced}
          <label class="block text-sm">
            <span class="text-slate-500">Repeat interval (seconds; blank = never re-notify)</span>
            <input class="mt-1 w-40 rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={rRepeat} placeholder="e.g. 3600" />
          </label>
        {/if}

        <fieldset class="text-sm">
          <span class="text-slate-500">Channels</span>
          {#if channels.length === 0}
            <p class="text-xs text-slate-400">No channels defined — create one first.</p>
          {:else}
            <div class="mt-1 flex flex-wrap gap-3">
              {#each channels as c (c.id)}
                <label class="flex items-center gap-1">
                  <input type="checkbox" checked={rChannels.includes(c.id)} onchange={() => (rChannels = toggle(rChannels, c.id))} />
                  {c.name}
                </label>
              {/each}
            </div>
          {/if}
        </fieldset>

        <label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={rEnabled} /> Enabled</label>

        <div class="flex gap-2">
          <button type="submit" disabled={rlBusy} class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white disabled:opacity-50">
            {rlBusy ? "Saving…" : "Save"}
          </button>
          <button type="button" class="rounded-lg border px-3 py-1 text-sm dark:border-slate-700" onclick={resetRuleForm}>Cancel</button>
        </div>
      </form>
    {/if}
  {:else}
    <!-- ============================= EVENTS ============================= -->
    <div class="flex flex-wrap items-center gap-3 text-sm">
      <label class="flex items-center gap-1">
        <span class="text-slate-500">Rule</span>
        <select class="rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={evRule}>
          <option value="">All</option>
          {#each rules as r (r.id)}<option value={r.id}>{r.name}</option>{/each}
        </select>
      </label>
      <label class="flex items-center gap-1">
        <span class="text-slate-500">Status</span>
        <select class="rounded border px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={evStatus}>
          <option value="">All</option>
          <option value="delivered">delivered</option>
          <option value="failed">failed</option>
          <option value="pending">pending</option>
        </select>
      </label>
      {#if summary}
        <span class="text-xs text-slate-500">
          {summary.delivered} delivered · <span class="text-amber-600">{summary.failed} failed</span> · {summary.pending} pending
        </span>
      {/if}
      <button class="rounded border px-2 py-1 text-xs dark:border-slate-700" onclick={loadEvents}>Refresh</button>
    </div>

    {#if evLoading}
      <p class="text-sm text-slate-500">Loading…</p>
    {:else if events.length === 0}
      <p class="text-sm text-slate-500">No events.</p>
    {:else}
      <table class="w-full text-sm">
        <thead class="text-left text-slate-500">
          <tr><th class="py-1">Occurred</th><th>Rule</th><th>Event</th><th>Library</th><th>Status</th><th>Delivered</th></tr>
        </thead>
        <tbody>
          {#each events as ev (ev.id)}
            <tr class="border-t border-slate-100 align-top dark:border-slate-800">
              <td class="py-1 whitespace-nowrap text-xs">{fmt(ev.occurred_at)}</td>
              <td>{ruleName(ev.rule_id)}</td>
              <td class="text-xs">{ev.event_type}</td>
              <td class="text-xs">{libName(ev.library_id)}</td>
              <td>
                <span
                  class="rounded px-1 text-xs {ev.status === 'failed'
                    ? 'bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300'
                    : ev.status === 'delivered'
                      ? 'bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300'
                      : 'bg-slate-100 text-slate-600 dark:bg-slate-800'}">
                  {ev.status}
                </span>
                {#if ev.last_error}
                  <button class="ml-1 text-xs underline" onclick={() => (expanded = { ...expanded, [ev.id]: !expanded[ev.id] })}>
                    {expanded[ev.id] ? "hide" : "why"}
                  </button>
                {/if}
                {#if ev.last_error && expanded[ev.id]}
                  <div class="mt-1 max-w-md break-words text-xs text-red-600">{ev.last_error}</div>
                {/if}
              </td>
              <td class="whitespace-nowrap text-xs">{fmt(ev.delivered_at)}{#if ev.delivery_attempts > 0} ({ev.delivery_attempts} tries){/if}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  {/if}
</div>
