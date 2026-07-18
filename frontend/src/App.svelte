<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import AdminPage from "./lib/AdminPage.svelte";
  import JobsPage from "./lib/JobsPage.svelte";
  import AlertsPage from "./lib/AlertsPage.svelte";
  import BrowsePage from "./lib/BrowsePage.svelte";
  import SearchPage from "./lib/SearchPage.svelte";
  import TimelinePage from "./lib/TimelinePage.svelte";
  import ReportsPage from "./lib/ReportsPage.svelte";
  import AgentsPage from "./lib/AgentsPage.svelte";
  import FilterBuilderPage from "./lib/FilterBuilderPage.svelte";
  import HelpPage from "./lib/HelpPage.svelte";
  import LoginPage from "./lib/LoginPage.svelte";
  import { getVersion, authStatus, authMe, authLogout, type AuthMode, type AuthPrincipal } from "./lib/api";
  import { theme, applyTheme } from "./lib/theme.svelte";
  import { shareFormat, setShareFormat, detectedPlatform } from "./lib/osFormat.svelte";
  import type { FormatPref } from "./lib/osFormat";
  import { parseBrowseHash } from "./lib/routes";

  // UI-T9 — hash-based routing so a refresh keeps the current tab and
  // back/forward toggles between them. `#/search` (default), `#/admin`,
  // `#/jobs` (UI-T10 jobs dashboard).
  type Page = "search" | "admin" | "jobs" | "alerts" | "browse" | "timeline" | "reports" | "agents" | "filter-builder" | "help";
  function routeFromHash(): { page: Page; browseLib: string; browsePath: string } {
    const browse = parseBrowseHash(location.hash);
    if (browse) return { page: "browse", browseLib: browse.libraryId, browsePath: browse.path };
    if (location.hash === "#/admin") return { page: "admin", browseLib: "", browsePath: "" };
    if (location.hash === "#/jobs") return { page: "jobs", browseLib: "", browsePath: "" };
    if (location.hash === "#/alerts") return { page: "alerts", browseLib: "", browsePath: "" };
    if (location.hash === "#/timeline") return { page: "timeline", browseLib: "", browsePath: "" };
    if (location.hash === "#/reports") return { page: "reports", browseLib: "", browsePath: "" };
    if (location.hash === "#/agents") return { page: "agents", browseLib: "", browsePath: "" };
    if (location.hash === "#/filter-builder") return { page: "filter-builder", browseLib: "", browsePath: "" };
    if (location.hash === "#/help") return { page: "help", browseLib: "", browsePath: "" };
    return { page: "search", browseLib: "", browsePath: "" };
  }

  // UI-T15: human label for the detected OS, shown in the "Auto" option so the
  // user knows which spelling Auto resolves to.
  const platformLabel =
    detectedPlatform === "windows"
      ? "Windows"
      : detectedPlatform === "mac"
        ? "Mac"
        : detectedPlatform === "linux"
          ? "Linux"
          : "other";

  const initialRoute = routeFromHash();
  let page = $state<Page>(initialRoute.page);
  let browseLib = $state<string>(initialRoute.browseLib);
  let browsePath = $state<string>(initialRoute.browsePath);
  // Running-build stamp (deploy-injected). Lets anyone check at a glance that
  // the page/backend match a specific deploy — pairs with the deploy verifier.
  let buildStamp = $state<string | null>(null);
  // AGPL §13 Source link target. Prefer the runtime value from /version
  // (FILEARR_SOURCE_URL) so a fork can point users at its modified source without
  // a frontend rebuild; fall back to the Vite build-time default.
  let sourceUrl = $state<string>(__SOURCE_URL__);
  // P5-T1/W6-D4: the distributed-agent feature is opt-in (FILEARR_AGENTS_ENABLED);
  // gate the Agents nav entry so it only shows when the surface is live.
  let agentsEnabled = $state(false);
  getVersion()
    .then((v) => {
      buildStamp = v.build_stamp;
      if (v.source_url) sourceUrl = v.source_url;
      agentsEnabled = !!v.agents_enabled;
    })
    .catch(() => {});

  // P6-T1 auth gate. `authMode` drives whether a login wall is shown; `me` is
  // the current session principal (null when signed out). `authReady` gates the
  // initial render so we never flash the app before knowing the mode.
  let authMode = $state<AuthMode>("disabled");
  let oidcEnabled = $state(false);
  let me = $state<AuthPrincipal | null>(null);
  let authReady = $state(false);

  async function probeAuth() {
    try {
      const st = await authStatus();
      authMode = st.mode;
      oidcEnabled = st.oidc_enabled;
      me = st.mode === "disabled" ? null : await authMe();
    } catch {
      // If the probe fails (e.g. backend unreachable), fail OPEN to the app
      // rather than trapping the user behind a wall we cannot verify.
      authMode = "disabled";
      me = null;
    } finally {
      authReady = true;
    }
  }
  probeAuth();

  // The wall shows when auth is active (enabled/bootstrap) and there is no
  // authenticated principal yet.
  const gated = $derived(authReady && authMode !== "disabled" && me === null);

  async function doLogout() {
    await authLogout();
    me = null;
    await probeAuth();
  }

  function goto(p: Page) {
    page = p;
    const hash = p === "search" ? "#/search" : `#/${p}`;
    if (location.hash !== hash) location.hash = hash;
  }

  // React to back/forward + manual hash edits. Guarded so it only reassigns when
  // the derived page actually changes (avoids redundant $state churn).
  function onHashChange() {
    const next = routeFromHash();
    if (next.page !== page) page = next.page;
    if (next.browseLib !== browseLib) browseLib = next.browseLib;
    if (next.browsePath !== browsePath) browsePath = next.browsePath;
  }

  function toggleDark() {
    theme.mode = document.documentElement.classList.contains("dark") ? "light" : "dark";
    applyTheme();
  }

  onMount(() => {
    applyTheme();
    // Normalise a bare URL to the default route so refresh/bookmark is stable.
    if (!location.hash) location.replace("#/search");
    window.addEventListener("hashchange", onHashChange);
  });

  onDestroy(() => window.removeEventListener("hashchange", onHashChange));
</script>

{#if gated}
  <LoginPage mode={authMode} {oidcEnabled} onAuthed={probeAuth} />
{:else}
<div class="mx-auto max-w-screen-2xl px-4 py-4 sm:px-6 lg:px-8">
  <header class="flex items-center gap-3 py-4">
    <h1 class="text-2xl font-bold" style="color: var(--accent)">Filearr</h1>
    <nav class="ml-4 flex gap-1">
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'search' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("search")}>Search</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'admin' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("admin")}>Admin</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'jobs' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("jobs")}>Jobs</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'alerts' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("alerts")}>Alerts</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'timeline' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("timeline")}>Timeline</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'reports' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("reports")}>Reports</button>
      {#if agentsEnabled}
        <button
          class="rounded-lg px-3 py-1 text-sm {page === 'agents' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
          onclick={() => goto("agents")}>Agents</button>
      {/if}
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'filter-builder' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("filter-builder")}>Filter builder</button>
    </nav>
    <div class="grow"></div>
    {#if me}
      <span class="rounded-lg bg-slate-100 px-2 py-1 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300"
        title={`${me.username} · ${me.global_role}`}>
        {me.username}<span class="ml-1 text-slate-400">({me.global_role})</span>
      </span>
      <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
        onclick={doLogout}>Sign out</button>
    {/if}
    <label class="flex items-center gap-1 text-xs text-slate-500" title="Which network-path spelling to show for Open/Copy actions. smb:// suits Linux/macOS; \\host\share (UNC) suits Windows.">
      <span class="hidden sm:inline">Paths</span>
      <select
        class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800"
        aria-label="Network path format"
        value={shareFormat.pref}
        onchange={(e) => setShareFormat((e.currentTarget as HTMLSelectElement).value as FormatPref)}>
        <option value="auto">Auto ({platformLabel})</option>
        <option value="url">smb:// URL</option>
        <option value="unc">Windows UNC</option>
      </select>
    </label>
    <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
      onclick={toggleDark}>Theme</button>
  </header>

  {#if page === "admin"}
    <AdminPage {me} authDisabled={authMode === "disabled"} />
  {:else if page === "jobs"}
    <JobsPage />
  {:else if page === "alerts"}
    <AlertsPage />
  {:else if page === "browse"}
    <BrowsePage libraryId={browseLib} path={browsePath} />
  {:else if page === "timeline"}
    <TimelinePage />
  {:else if page === "reports"}
    <ReportsPage />
  {:else if page === "agents"}
    <AgentsPage />
  {:else if page === "filter-builder"}
    <FilterBuilderPage />
  {:else if page === "help"}
    <HelpPage />
  {:else}
    <SearchPage />
  {/if}

  <footer class="mt-12 border-t border-slate-200 py-4 text-xs text-slate-500 dark:border-slate-800">
    <span title={buildStamp ?? "no build stamp (dev)"}>
      Filearr {__APP_VERSION__}{#if buildStamp}
        <span class="font-mono text-slate-400"> · build {buildStamp.slice(0, 12)}</span>
      {/if}
    </span>
    <span class="px-1">·</span>
    <button
      type="button"
      class="underline hover:text-[var(--accent)]"
      onclick={() => goto("help")}>Help</button>
    <span class="px-1">·</span>
    <a
      class="underline hover:text-[var(--accent)]"
      href="/api/docs"
      target="_blank"
      rel="noopener noreferrer">API Docs</a>
    <span class="px-1">·</span>
    <!-- AGPL-3.0 §13: offer the running instance's Corresponding Source. -->
    <a
      class="underline hover:text-[var(--accent)]"
      href={sourceUrl}
      target="_blank"
      rel="noopener noreferrer">Source</a>
    <span class="px-1">·</span>
    <span>AGPL-3.0-or-later</span>
  </footer>
</div>
{/if}
