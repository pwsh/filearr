<script lang="ts">
  import { authLogin, authBootstrap, oidcLoginUrl, ApiError, type AuthMode } from "./api";

  // `mode` is "bootstrap" (no users yet → create the first admin) or "enabled"
  // (normal login). `onAuthed` is called after a successful login/bootstrap so
  // the shell can re-probe and drop the wall.
  let {
    mode,
    onAuthed,
    oidcEnabled = false,
  }: { mode: AuthMode; onAuthed: () => void; oidcEnabled?: boolean } = $props();

  let username = $state("");
  let password = $state("");
  let error = $state<string | null>(null);
  let warning = $state<string | null>(null);
  let busy = $state(false);

  // Surfaced when the page itself is being served over plain http — the session
  // cookie will not carry the Secure flag, so nudge the operator to the TLS port.
  const insecure = typeof location !== "undefined" && location.protocol === "http:";

  // Surfaced when an OIDC callback bounced back with ?sso_error=<reason> (the
  // server never leaks detail — this is a generic, friendly message).
  const ssoError =
    typeof location !== "undefined"
      ? new URLSearchParams(location.search).get("sso_error")
      : null;

  function startSso() {
    // A real top-level navigation (not fetch) so the IdP round-trip + the
    // Set-Cookie on the callback return actually take effect.
    window.location.assign(oidcLoginUrl("/"));
  }

  const isBootstrap = $derived(mode === "bootstrap");

  async function submit(e: Event) {
    e.preventDefault();
    error = null;
    warning = null;
    busy = true;
    try {
      if (isBootstrap) {
        await authBootstrap(username.trim(), password);
        // First admin created — log straight in to establish the session.
        await authLogin(username.trim(), password);
      } else {
        const res = await authLogin(username.trim(), password);
        warning = res.warning;
      }
      onAuthed();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (err instanceof ApiError && err.status === 429) {
        // P6-T8: generic lockout message (never reveals whether the username
        // exists, nor the exact remaining time beyond the server's Retry-After).
        error = "Too many failed attempts. Please wait a few minutes and try again.";
      } else if (isBootstrap) {
        error = `Could not create the first admin: ${msg}`;
      } else {
        error = "Invalid username or password.";
      }
    } finally {
      busy = false;
    }
  }
</script>

<div class="mx-auto mt-24 max-w-sm rounded-2xl border border-slate-200 p-6 shadow-sm dark:border-slate-800">
  <h1 class="mb-1 text-2xl font-bold" style="color: var(--accent)">Filearr</h1>
  <p class="mb-4 text-sm text-slate-500">
    {isBootstrap ? "First run — create the administrator account." : "Sign in to continue."}
  </p>

  {#if insecure}
    <div class="mb-4 rounded-lg bg-amber-100 px-3 py-2 text-xs text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
      You are on plain <strong>http</strong>. Credentials and the session cookie
      are not protected in transit — use the TLS endpoint
      (<span class="font-mono">https://&lt;host&gt;:8443</span>) once available.
    </div>
  {/if}

  {#if ssoError}
    <div class="mb-4 rounded-lg bg-red-100 px-3 py-2 text-xs text-red-800 dark:bg-red-900/40 dark:text-red-200">
      Single sign-on did not complete. Please try again or use a local account.
    </div>
  {/if}

  {#if oidcEnabled}
    <button
      type="button"
      onclick={startSso}
      class="mb-3 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800">
      Sign in with SSO
    </button>
    <div class="mb-3 flex items-center gap-2 text-xs text-slate-400">
      <span class="h-px flex-1 bg-slate-200 dark:bg-slate-700"></span>
      or
      <span class="h-px flex-1 bg-slate-200 dark:bg-slate-700"></span>
    </div>
  {/if}

  <form onsubmit={submit} class="flex flex-col gap-3">
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-slate-500">Username</span>
      <input
        class="rounded-lg border border-slate-300 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
        bind:value={username}
        autocomplete="username"
        required />
    </label>
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-slate-500">Password</span>
      <input
        class="rounded-lg border border-slate-300 px-3 py-2 dark:border-slate-700 dark:bg-slate-900"
        type="password"
        bind:value={password}
        autocomplete={isBootstrap ? "new-password" : "current-password"}
        required />
    </label>

    {#if isBootstrap}
      <p class="text-xs text-slate-400">Minimum 8 characters.</p>
    {/if}

    {#if error}
      <p class="text-sm text-red-600">{error}</p>
    {/if}
    {#if warning}
      <p class="text-sm text-amber-600">{warning}</p>
    {/if}

    <button
      class="mt-1 rounded-lg bg-[var(--accent)] px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
      type="submit"
      disabled={busy}>
      {busy ? "Please wait…" : isBootstrap ? "Create admin & sign in" : "Sign in"}
    </button>
  </form>
</div>
