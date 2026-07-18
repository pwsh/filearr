// Thin client for the Filearr API (search goes through the backend, which
// translates flat params into Meilisearch filter syntax).

// A single search hit is an untyped Meili document plus, for document results,
// P3-T5 ``snippet`` (cropped body text with <em>…</em> match markers) and
// ``highlight`` (title/filename markers). Both are rendered SAFELY on the client
// (text nodes + <mark>, never {@html}); the raw ``body_text`` is stripped
// server-side so a response never ships kilobytes of body per row.
export type SearchHit = Record<string, unknown> & {
  snippet?: string;
  highlight?: { title?: string; filename?: string };
};

export interface SearchResponse {
  hits: SearchHit[];
  total: number;
  facets: Record<string, Record<string, number>>;
  // P3-T4: per-numeric-facet min/max from Meili facetStats (size/mtime). Empty
  // when the engine returns no stats (e.g. an empty result set). Drives the
  // range-slider bounds — never hardcoded.
  facet_stats: Record<string, { min: number; max: number }>;
  next_cursor: string | null;
}

const KEY = () => localStorage.getItem("apiKey") ?? "";

/** The stored API key, if any. Empty string when auth is disabled / no key set. */
export const apiKey = (): string => KEY();

/** API base path (shared by fetch requests and the SSE EventSource URL). */
export const API_BASE = "/api/v1";

/** Immutable, content-addressed thumbnail URL for an item + tier (S12/P12).
 *  Used as an ``<img src>``, which cannot send an Authorization header, so the
 *  read-scope key rides as ``?api_key=`` exactly like the SSE events stream.
 *  ``tier`` is one of the serve-endpoint enum values ("grid" | "preview"). */
export function thumbUrl(id: string, tier: "grid" | "preview" = "grid"): string {
  const key = KEY();
  const auth = key ? `&api_key=${encodeURIComponent(key)}` : "";
  return `${API_BASE}/items/${id}/thumb?tier=${tier}${auth}`;
}

/** An HTTP error carrying the numeric status, so callers can branch on 401/403/
 *  404 (P6-T4 RBAC). ``message`` keeps the legacy ``"<status>: <body>"`` shape
 *  so existing ``String(e)`` render paths are unchanged. */
export class ApiError extends Error {
  status: number;
  body: string;
  constructor(status: number, body: string) {
    super(`${status}: ${body}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

/** Map an error to a friendly, non-technical sentence for RBAC-denied surfaces
 *  (403 = permission, 404 = not visible/gone) — so a scoped user sees a clear
 *  message instead of a blank pane or a raw status dump. Falls back to the raw
 *  message for anything else. */
export function friendlyError(e: unknown, verb = "view"): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return `You don't have permission to ${verb} this item.`;
    if (e.status === 404) return "This item is not available (it may be outside your access or removed).";
    if (e.status === 401) return "Please sign in to continue.";
  }
  return String(e);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}),
      ...init?.headers,
    },
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json() as Promise<T>;
}

export function search(
  params: Record<string, string>,
  signal?: AbortSignal,
): Promise<SearchResponse> {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== ""));
  return request(`/search?${qs}`, { signal });
}

export function patchItem(id: string, patch: Record<string, unknown>) {
  return request(`/items/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

/** A full single-item record: every stored column, with ``metadata`` and
 *  ``user_metadata`` returned as separate unmerged objects. Backs the Raw tab. */
export type ItemRecord = Record<string, unknown> & {
  // P10-T11/T12: the item's resolved network location (e.g. ``smb://…``) and the
  // tier that produced it. Resolution precedence: agent hint > admin mapping >
  // library share_prefix. ``share_url`` is null when no location resolves — the
  // UI then renders NO open affordance (never a fabricated/empty location).
  share_url?: string | null;
  share_source?: "agent_hint" | "mapping" | "library" | null;
};

/** Fetch one item with every stored field. Powers the Raw detail view. */
export const getItem = (id: string) => request<ItemRecord>(`/items/${id}`);

/** P10-T3: ask the owning agent to re-verify an agent-hosted item's existence
 *  (``stat``) or integrity (``rehash``). Returns the created agent_commands row;
 *  the result lands later via a normal item refresh once the agent completes it. */
export const verifyItem = (id: string, mode: "stat" | "rehash") =>
  request<{ id: string; kind: string; status: string; mode: string }>(
    `/items/${id}/verify`,
    { method: "POST", body: JSON.stringify({ mode }) },
  );

// ---- P10-T10 — hosting-agent identity / online status / verify freshness -----
/** ``GET /items/{id}/agent-status``. For a centrally-scanned item only
 *  ``{agent_hosted:false}`` comes back; for an agent-hosted item the panel fields
 *  are present. ``online`` is ``last_seen_at`` within the server's online window. */
export interface ItemAgentStatus {
  agent_hosted: boolean;
  agent_id?: string;
  agent_name?: string;
  agent_status?: "active" | "revoked" | "pending";
  online?: boolean;
  last_seen_at?: string | null;
  last_verified_at?: string | null;
  verify_in_flight?: boolean;
}

export const itemAgentStatus = (id: string) =>
  request<ItemAgentStatus>(`/items/${id}/agent-status`);

// ---- P10-T6/T7/T13 — agent file retrieve (transfer) --------------------------
export type TransferState =
  | "pending"
  | "uploading"
  | "staged"
  | "downloaded"
  | "expired"
  | "failed";

/** ``GET /transfers/{id}`` status payload (also the shape the SSE frames mirror). */
export interface TransferStatus {
  transfer_id: string;
  item_id: string;
  agent_id: string;
  state: TransferState;
  verified: boolean;
  bytes_transferred: number;
  total_bytes: number | null;
  created_at: string | null;
  expires_at: string | null;
  last_range_request_at: string | null;
}

/** One SSE frame: the status payload + the P10-T7 derived ``waiting_for_agent``
 *  pseudo-state, plus ``reason`` (terminal frames) / ``detail`` (error frames). */
export interface TransferEvent extends Partial<TransferStatus> {
  waiting_for_agent?: boolean;
  reason?: string;
  detail?: string;
}

/** Initiate an agent→central retrieve (P10-T13). A 202 returns the new transfer;
 *  a 409 "an active transfer already exists" is NOT an error — its id is parsed
 *  out of the detail so the caller attaches to the in-flight transfer instead
 *  (``existing: true``). Any other failure propagates. */
export async function initiateTransfer(
  itemId: string,
  verifyHash = true,
): Promise<{ transfer_id: string; state: string; existing: boolean }> {
  try {
    const r = await request<{ transfer_id: string; state: string }>(
      `/items/${itemId}/transfer`,
      { method: "POST", body: JSON.stringify({ verify_hash: verifyHash }) },
    );
    return { ...r, existing: false };
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      const m = e.body.match(
        /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i,
      );
      if (m) return { transfer_id: m[0], state: "pending", existing: true };
    }
    throw e;
  }
}

export const getTransfer = (id: string) =>
  request<TransferStatus>(`/transfers/${id}`);

export const cancelTransfer = (id: string) =>
  request<{ transfer_id: string; state: string }>(`/transfers/${id}`, {
    method: "DELETE",
  });

/** SSE URL for a transfer's progress stream. ``EventSource`` can't set headers,
 *  so the read-scope key rides as ``?api_key=`` exactly like the scans SSE. */
export function transferEventsUrl(id: string): string {
  const key = KEY();
  const qs = key ? `?api_key=${encodeURIComponent(key)}` : "";
  return `${API_BASE}/transfers/${id}/events${qs}`;
}

/** Fetch the verified staged file (auth header) and save it as ``filename``.
 *  Mirrors ``downloadExport`` — a blob save so the Bearer header is sent (an
 *  ``<a href>`` cannot), served only for a verified, staged/downloaded transfer. */
export async function downloadTransfer(id: string, filename: string): Promise<void> {
  const res = await fetch(`${API_BASE}/transfers/${id}/download`, {
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  await saveBlob(res, filename || `transfer-${id}`);
}

// P3-T9 — related / near-duplicate items via the semantic vector. Returns 409
// (thrown as an error by ``request``) when semantic search is disabled server-side
// or the item is not yet embedded; callers treat that as "unavailable".
export interface SimilarResponse {
  id: string;
  hits: SearchHit[];
}
export const similarItems = (id: string, limit = 10) =>
  request<SimilarResponse>(`/items/${id}/similar?limit=${limit}`);

// P3-T8 — the /stats semantic coverage section (present but disabled=false when
// FILEARR_SEMANTIC_ENABLED is off). Drives the hidden-unless-enabled UI affordances.
export interface SemanticStats {
  enabled: boolean;
  model: string;
  embedded_count: number;
  pending: number;
  fp_mismatches: number;
}
export const semanticStats = async (): Promise<SemanticStats | null> => {
  try {
    const r = (await stats()) as { semantic?: SemanticStats };
    return r.semantic ?? null;
  } catch {
    return null;
  }
};

// ---- admin ----
export type HashPolicy = "auto" | "full" | "quick_only";

// FIX-10: most-recent ScanRun for a library, sourced per-library from scan_runs
// (survives redeploys; not subject to the capped global /scans feed). Null only
// when the library has genuinely never been scanned.
export interface LastScan {
  started_at: string;
  finished_at: string | null;
  status: string;
  seen?: number | null;
  new?: number | null;
  changed?: number | null;
  missing?: number | null;
}

export interface Library {
  id: string;
  name: string;
  root_path: string;
  native_prefix: string | null;
  share_prefix: string | null;
  enabled_types: string[];
  include_globs: string[];
  exclude_globs: string[];
  enabled_presets: string[];
  enabled_extension_groups: string[];
  scan_cron: string | null;
  watch_mode: boolean;
  hash_policy: HashPolicy;
  hash_full_max_bytes: number | null;
  ocr_enabled: boolean;
  expose_gps: boolean;
  enabled: boolean;
  last_scan: LastScan | null;
  // OPS-T7: effective user-facing share prefix + provenance. ``share_prefix``
  // above is the raw manual override; these are computed server-side (manual
  // wins, else the deploy mount map covering the library root, else none).
  share_prefix_effective: string | null;
  share_prefix_source: "manual" | "mount-map" | "none";
  // UI-T15: Windows-UNC counterpart of ``share_prefix_effective`` (null when the
  // location has no UNC form). The UI renders whichever spelling the viewer's OS
  // wants; see lib/osFormat.ts.
  share_unc_effective: string | null;
}

export interface ScanRun {
  id: string;
  library_id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  stats: Record<string, number>;
}

// ---- P2-T5 presets / extension groups (read-only catalogue) ----
export interface Preset {
  name: string;
  label: string;
  patterns: string[];
  default_enabled: boolean;
  caveat: string | null;
}

export interface ExtensionGroup {
  name: string;
  label: string;
  media_type: string;
  extensions: string[];
}

export interface PresetsResponse {
  presets: Preset[];
  extension_groups: ExtensionGroup[];
}

/** The code-constant preset bundles + extension groups (read scope). */
export const listPresets = () => request<PresetsResponse>("/presets");

export const listLibraries = () => request<Library[]>("/libraries");

// OPS-T7: the deploy-time network-share mount map (read scope). Credential-free;
// ``share_url`` is a user-facing reference the library form surfaces as a hint.
export interface ShareMapEntry {
  container_prefix: string;
  share_url: string;
  storage_type: string | null;
  host: string | null;
  unc?: string | null;
}

export const listShareMap = () => request<ShareMapEntry[]>("/system/share-map");

/** Longest-container_prefix-wins client mirror of share_map.resolve — used to
 *  preview the auto share_prefix a library root would inherit from the deploy. */
export function resolveShareHint(
  map: ShareMapEntry[],
  rootPath: string,
): ShareMapEntry | null {
  const norm = (p: string) => p.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  const target = norm(rootPath);
  let best: ShareMapEntry | null = null;
  let bestLen = -1;
  for (const e of map) {
    const base = norm(e.container_prefix);
    const covers = base === "" || target === base || target.startsWith(base + "/");
    if (covers && base.length > bestLen) {
      best = e;
      bestLen = base.length;
    }
  }
  if (!best) return null;
  // Append the mount-relative remainder so a library rooted at a SUBFOLDER of
  // the mount shows its true network location (mirror of backend
  // share_map.resolve): mount /data/media -> smb://server/share with root
  // /data/media/information must hint smb://server/share/information.
  const baseSegs = norm(best.container_prefix).split("/").filter(Boolean);
  const remainder = target.split("/").filter(Boolean).slice(baseSegs.length);
  if (remainder.length === 0) return best;
  const joinUrl = (prefix: string, sep: string) =>
    prefix.replace(new RegExp(`[${sep === "\\" ? "\\\\" : sep}/]+$`), "") +
    sep +
    remainder.join(sep);
  return {
    ...best,
    share_url: joinUrl(best.share_url, "/"),
    unc: best.unc ? joinUrl(best.unc, "\\") : best.unc,
  };
}

export const createLibrary = (body: {
  name: string;
  root_path: string;
  native_prefix?: string | null;
  share_prefix?: string | null;
  enabled_types?: string[];
  include_globs?: string[];
  exclude_globs?: string[];
  enabled_presets?: string[];
  enabled_extension_groups?: string[];
  scan_cron?: string | null;
  watch_mode?: boolean;
  hash_policy?: HashPolicy;
  hash_full_max_bytes?: number | null;
  ocr_enabled?: boolean;
  expose_gps?: boolean;
}) => request<Library>("/libraries", { method: "POST", body: JSON.stringify(body) });

// Partial update (scan_cron / watch_mode edits are re-validated server-side; a
// 422 body carries the reason, surfaced by AdminPage's error banner).
export const updateLibrary = (
  id: string,
  patch: Partial<{
    name: string;
    root_path: string;
    native_prefix: string | null;
    share_prefix: string | null;
    enabled_types: string[];
    include_globs: string[];
    exclude_globs: string[];
    enabled_presets: string[];
    enabled_extension_groups: string[];
    scan_cron: string | null;
    watch_mode: boolean;
    hash_policy: HashPolicy;
    hash_full_max_bytes: number | null;
    ocr_enabled: boolean;
    expose_gps: boolean;
    enabled: boolean;
  }>,
) => request<Library>(`/libraries/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

/**
 * UI-T2 — hard-delete a library (admin scope). The contract requires an exact
 * name match in `?confirm=`; the server returns 204 on success, 409 while a scan
 * is running, 422 on a confirm mismatch, 404 if unknown. `request()` can't be
 * reused because a 204 carries no JSON body, so we fetch directly and translate
 * the status codes into a thrown Error the dialog can classify.
 */
export async function deleteLibrary(id: string, confirm: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/libraries/${id}?confirm=${encodeURIComponent(confirm)}`,
    {
      method: "DELETE",
      headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
    },
  );
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

// ---- P4-T3 custom fields (admin-defined user_metadata field definitions) ----
export type CustomFieldType =
  | "string" | "integer" | "float" | "boolean" | "date" | "url" | "select";

export const CUSTOM_FIELD_TYPES: CustomFieldType[] = [
  "string", "integer", "float", "boolean", "date", "url", "select",
];

export interface CustomField {
  id: string;
  name: string;
  label: string;
  data_type: CustomFieldType;
  select_options: string[] | null;
  applies_to: string[];
  library_ids: string[];
  facetable: boolean;
  sortable: boolean;
  required: boolean;
  created_at: string;
}

export const listCustomFields = () => request<CustomField[]>("/custom-fields");

export const createCustomField = (body: {
  name: string;
  label: string;
  data_type: CustomFieldType;
  select_options?: string[] | null;
  applies_to?: string[];
  library_ids?: string[];
  facetable?: boolean;
  sortable?: boolean;
  required?: boolean;
}) => request<CustomField>("/custom-fields", { method: "POST", body: JSON.stringify(body) });

// PATCH: name/data_type are IMMUTABLE server-side (a 422 rejects them); only the
// mutable fields below should ever be sent.
export const updateCustomField = (
  id: string,
  patch: Partial<{
    label: string;
    select_options: string[] | null;
    applies_to: string[];
    library_ids: string[];
    facetable: boolean;
    sortable: boolean;
    required: boolean;
  }>,
) => request<CustomField>(`/custom-fields/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

// Soft-delete: drops the definition; existing user_metadata values are untouched.
export async function deleteCustomField(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/custom-fields/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

export const scanLibrary = (id: string) =>
  request<{ job_id: number }>(`/libraries/${id}/scan`, { method: "POST" });

export const listScans = () => request<ScanRun[]>("/scans");

export const stats = () => request<Record<string, unknown>>("/stats");

export const cancelScan = (id: string) =>
  request<{ status: string }>(`/scans/${id}/cancel`, { method: "POST" });

// UI-T13 graceful stop: finish the current batch + wrap-up (no tombstoning),
// keep everything scanned so far, end the run "stopped". Distinct from cancel
// (hard abort). Idempotent server-side; 409 if the scan is not running.
export const stopScan = (id: string) =>
  request<{ status: string }>(`/scans/${id}/stop`, { method: "POST" });

// FIX-15 force-clear: an admin escape hatch for a ScanRun wedged non-terminal
// ('stopping' that was never observed, or 'running' orphaned by a dead worker).
// Drives it terminal ('stopped'); refuses (409) only a genuinely-active run
// (live worker present -> use stopScan). Admin-scoped + audited server-side.
export const forceClearScan = (id: string) =>
  request<{ status: string; previous_status: string }>(
    `/scans/${id}/force-clear`,
    { method: "POST" },
  );

/**
 * URL for the scan-progress SSE stream. `EventSource` cannot set an
 * Authorization header, so when an API key is present we pass it as the
 * `api_key` query param — the backend accepts it ONLY on this read-only events
 * endpoint. When auth is disabled (dev) no key is appended.
 */
export function scanEventsUrl(id: string): string {
  const key = KEY();
  const qs = key ? `?api_key=${encodeURIComponent(key)}` : "";
  return `${API_BASE}/scans/${id}/events${qs}`;
}

// ---- UI-T4 server-side folder browser ----
export interface FsEntry {
  name: string;
  path: string;
}

/** `GET /fs/browse` payload: an allowlisted, symlink-safe directory listing.
 *  Empty `path` lists the configured roots. `parent` is null at a root. */
export interface FsBrowse {
  path: string;
  parent: string | null;
  roots: string[];
  dirs: FsEntry[];
}

/** Browse directories under the server's allowlist. A path outside the allowlist
 *  (or a traversal attempt) yields a 422 the picker surfaces inline. */
export const browseFs = (path = "") =>
  request<FsBrowse>(`/fs/browse?path=${encodeURIComponent(path)}`);

// ---- T11 error surfacing ----
export interface FailingItem {
  id: string;
  rel_path: string;
  error: string;
}

export interface LibraryErrors {
  library_id: string;
  count: number;
  items: FailingItem[];
}

export interface FailedJob {
  id: string;
  queue: string;
  task: string;
  status: string;
  attempts: number | null;
  /** FIX-12: task's genuine-failure retry budget (null = no retry). */
  retry_cap: number | null;
  scheduled_at: string | null;
  attempted_at: string | null;
  error: string | null;
}

/** Paginated failed-jobs response (FIX-8). ``total`` is the full failed-row
 *  count so the UI can render a real pager; ``items`` is the requested page. */
export interface FailedJobPage {
  items: FailedJob[];
  total: number;
  limit: number;
  offset: number;
}

/** Per-library extraction-error count + a paginated page of failing items. */
export const libraryErrors = (id: string, limit = 50, offset = 0) =>
  request<LibraryErrors>(`/libraries/${id}/errors?limit=${limit}&offset=${offset}`);

/** Clear stored extraction errors for a library and re-defer extraction for the
 *  affected items (plus any never-hashed items). Returns the number requeued. */
export const retryExtracts = (id: string) =>
  request<{ library_id: string; retried: number }>(
    `/libraries/${id}/retry-extracts`,
    { method: "POST" },
  );

/** Paginated failed Procrastinate jobs (read scope; page capped server-side at
 *  100). Returns {items, total, limit, offset} so the caller can render a pager
 *  (FIX-8 — the list used to grow unbounded on screen). */
export const failedJobs = (limit = 25, offset = 0) =>
  request<FailedJobPage>(`/system/failed-jobs?limit=${limit}&offset=${offset}`);

/** Delete failed Procrastinate rows now (FIX-8, admin scope). Optional ``queue``
 *  scopes the wipe to one queue. Returns the number deleted. */
export const clearFailedJobs = (queue?: string) =>
  request<{ deleted: number; queue: string | null }>(
    "/system/jobs/clear-failed",
    { method: "POST", body: JSON.stringify(queue ? { queue } : {}) },
  );

/** Running-build identity: package version + deploy build stamp (null in dev) +
 *  AGPL §13 source_url (FILEARR_SOURCE_URL). */
export const getVersion = () =>
  request<{
    app_version: string;
    build_stamp: string | null;
    source_url?: string;
    agents_enabled?: boolean; // P5-T1: gates the Admin -> Agents panel
  }>("/version");

// ---- UI-T10 jobs dashboard ----
export interface RunningJob {
  id: string;
  queue: string;
  task: string;
  args: Record<string, unknown>;
  started_at: string | null;
  seconds_running: number | null;
  attempts: number;
  /** FIX-12: task's genuine-failure retry budget (null = no retry). */
  retry_cap: number | null;
  worker_id: number | null;
  worker_alive: boolean;
  stalled: boolean;
  rel_path: string | null;
  /** File size of the job's item (bytes), when it carries a resolvable item_id.
   *  Null otherwise. The UI appends it to thumbnail rows (size predicts duration). */
  size: number | null;
  library_name: string | null;
}

export interface ScanRunning {
  id: string;
  library_id: string;
  library_name: string;
  rel_path: string | null;
  stats: Record<string, number>;
}

export interface MeiliSnapshot {
  healthy: boolean;
  document_count: number | null;
  is_indexing: boolean | null;
  postgres_active: number;
  drift: number | null;
  in_sync: boolean | null;
}

export interface ExtractSummary {
  depth: number;
  running: number;
  done: number;
  failed: number;
}

export interface StalledSummary {
  total: number;
  by_queue: Record<string, number>;
}

/** Coarse CPU-load indicator riding the Jobs poll (NOT a metrics system). All
 *  fields are null on a host without `os.getloadavg` (Windows / restricted). */
export interface CpuLoad {
  load1: number | null;
  load5: number | null;
  load15: number | null;
  cores: number | null;
  /** 100 * load1 / cores — may exceed 100 under overload (not clamped). */
  percent: number | null;
}

/** Cumulative disk I/O byte counters (from /proc/diskstats). Rates are computed
 *  client-side between polls. Null off Linux / when /proc is unreadable. */
export interface IoCounters {
  read_bytes: number;
  write_bytes: number;
}

/** Cumulative network byte counters (from /proc/net/dev, all interfaces but lo).
 *  Rates are computed client-side between polls. Null off Linux. */
export interface NetCounters {
  rx_bytes: number;
  tx_bytes: number;
}

/** Cheap Postgres health snapshot (a few catalog reads). Null on any failure
 *  (permissions / odd PG / bare DB) so the tile simply hides. */
export interface DbHealth {
  backends: number;
  active: number;
  idle_in_tx: number;
  waiting: number;
  longest_query_s: number;
  longest_idle_in_tx_s: number;
  /** blks_hit / (hit + read); null when the denominator is 0. */
  cache_hit_ratio: number | null;
  deadlocks: number;
  temp_files: number;
  temp_bytes: number;
  xact_commit: number;
  xact_rollback: number;
  /** Total procrastinate `todo` backlog across queues. */
  queue_backlog: number;
}

/** Resource-load section of the Jobs summary. `io`/`net`/`db` are null when
 *  unavailable (non-Linux host, or a failed DB probe) — the tiles self-hide. */
export interface ResourcesSummary {
  cpu: CpuLoad;
  io: IoCounters | null;
  net: NetCounters | null;
  db: DbHealth | null;
}

/** One upcoming scheduled job (per-queue `upcoming` lists, soonest first). */
export interface UpcomingJob {
  label: string;
  /** ISO8601 next-fire instant. */
  at: string;
  task: string;
}

/** Thumbnail-creation monitor: whole-cache totals + the (configurable) thumbs
 *  queue snapshot re-exposed under a stable key. */
export interface ThumbsSummary {
  generated: number;
  bytes: number;
  failed_jobs: number;
  queue: Record<string, number>;
}

export interface JobsSummary {
  queues: Record<string, Record<string, number>>;
  extract: ExtractSummary;
  running: RunningJob[];
  failed_recent: FailedJob[];
  meili: MeiliSnapshot;
  scans_running: ScanRunning[];
  stalled: StalledSummary;
  /** UI-T14 — per-task-class default job priorities (higher = runs sooner). */
  priorities: Record<string, number>;
  /** UI-T14 — whether the staged scan→extract pipeline is enabled. */
  staged_pipeline: boolean;
  /** FIX-11 — disk-headroom rollup for the low-space banner (piggybacks the
   *  existing Jobs poll). `low` lists only the non-ok watch paths; `paths` is the
   *  full per-path detail for the always-on space indicator. */
  disk: DiskSummary;
  /** Coarse CPU/resource-load indicator (rides the same poll). */
  resources: ResourcesSummary;
  /** Thumbnail-creation monitor (rides the same poll). */
  thumbs: ThumbsSummary;
  /** Per-queue upcoming scheduled work (≤3 soonest each). Absent/empty queues
   *  render no "Upcoming" block. */
  upcoming: Record<string, UpcomingJob[]>;
}

/** FIX-11 — one monitored filesystem's headroom + policy verdict. */
export interface DiskPathStatus {
  label: string;
  path: string;
  free: number;
  total: number;
  pct_free: number;
  status: "ok" | "warn" | "critical";
  reason: string;
}

/** FIX-11 — Jobs-banner disk rollup (worst status + the non-ok paths only). The
 *  monitor additions also carry `paths`: the full per-path detail (every watch
 *  target, with `used`/`is_pg`) the always-on space indicator renders. `paths`
 *  is optional so an older backend (banner-only payload) never breaks the UI. */
export interface DiskSummary {
  status: "ok" | "warn" | "critical";
  low: DiskPathStatus[];
  paths?: (DiskPathStatus & {
    used: number;
    is_pg: boolean;
    /** Device-dedupe: the watch roles sharing this physical device (tooltip). */
    members?: { label: string; path: string }[];
  })[];
}

/** FIX-11 — full /system/disk payload (every path, not just the low ones). */
export interface DiskReport {
  status: "ok" | "warn" | "critical";
  paths: (DiskPathStatus & { used: number; is_pg: boolean })[];
}

export const systemDisk = () => request<DiskReport>("/system/disk");

/** Result of a runtime queue-priority bump (UI-T14, admin scope). */
export interface JobPriorityResult {
  queue: string;
  priority: number;
  updated: number;
}

/** Counts returned by the stalled-job reaper (FIX-6). */
export interface ReapResult {
  reaped: number;
  retried: number;
  failed: number;
  pruned_workers: number;
}

/** One composite snapshot for the Jobs tab (read scope). The dashboard polls
 *  THIS single URL every few seconds while the tab is visible. */
export const jobsSummary = () => request<JobsSummary>("/system/jobs/summary");

/** In-flight jobs only (read scope). Rarely needed directly — `jobsSummary`
 *  embeds the same list under `running`. */
export const runningJobs = () => request<RunningJob[]>("/system/jobs/running");

/** Requeue or fail jobs orphaned in `doing` by a dead/restarted worker
 *  (FIX-6, admin scope). Returns the reap counts. */
export const reapStalledJobs = () =>
  request<ReapResult>("/system/jobs/reap", { method: "POST" });

/** Re-prioritise a queue's PENDING (todo) jobs (UI-T14, admin scope). `priority`
 *  is clamped server-side to -100..100; higher runs sooner. Running jobs are
 *  unaffected. Returns the affected row count. */
export const setJobPriority = (queue: string, priority: number) =>
  request<JobPriorityResult>("/system/jobs/priority", {
    method: "POST",
    body: JSON.stringify({ queue, priority, scope: "pending" }),
  });


// ---- UI-T12 in-page folder navigation (browse tree) ----
export interface TreeFolder {
  name: string;
  item_count: number;
}

export interface TreeItem {
  id: string;
  rel_path: string;
  filename: string;
  media_type: string;
  size: number;
  title: string | null;
  year: number | null;
}

export interface TreeResponse {
  library_id: string;
  library_name: string;
  path: string;
  folders: TreeFolder[];
  folders_total: number;
  folders_offset: number;
  items: TreeItem[];
  total_items: number;
}

/** Browse a library's folder tree (read scope). `path` is a rel_path ('' = root);
 *  a traversal/absolute path yields a 422 the browse view surfaces inline. */
export const libraryTree = (
  id: string,
  path = "",
  limit = 100,
  offset = 0,
  foldersOffset = 0,
) =>
  request<TreeResponse>(
    `/libraries/${id}/tree?path=${encodeURIComponent(path)}&limit=${limit}&offset=${offset}&folders_offset=${foldersOffset}`,
  );


// ---- P3-T7 saved searches (named, persisted /search queries) ----
export interface SavedSearch {
  id: string;
  name: string;
  /** The flat /search params bundle, stored verbatim and replayed via /search. */
  params: Record<string, string>;
  owner_principal: string | null;
  created_at: string;
  updated_at: string;
}

export const listSavedSearches = () => request<SavedSearch[]>("/saved-searches");

export const createSavedSearch = (body: {
  name: string;
  params: Record<string, unknown>;
  owner_principal?: string | null;
}) => request<SavedSearch>("/saved-searches", { method: "POST", body: JSON.stringify(body) });

// PATCH: rename and/or replace params. An unknown param key is a 422 server-side.
export const updateSavedSearch = (
  id: string,
  patch: Partial<{ name: string; params: Record<string, unknown> }>,
) => request<SavedSearch>(`/saved-searches/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

export async function deleteSavedSearch(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/saved-searches/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

// ---- P4-T1 metadata profiles (read-only field vocabulary for key-facts) ----
export interface MetadataProfileField {
  type: string;
  label: string;
  required: boolean;
  facetable: boolean;
  sortable: boolean;
}

export interface MetadataProfile {
  id: string;
  media_type: string;
  version: number;
  created_at: string;
  /** field name -> declared shape (label/type/hints). NOTE: JSONB key order is
   *  not guaranteed to equal the code-declared FieldSpec order (P4-T12 known
   *  limitation) — the key-facts component orders by this map's iteration. */
  fields: Record<string, MetadataProfileField>;
}

/** One profile by media type; 404 for an unknown/unseeded type (callers treat a
 *  failure as "no profile" and fall back to raw key names). */
export const getMetadataProfile = (mediaType: string) =>
  request<MetadataProfile>(`/metadata-profiles/${encodeURIComponent(mediaType)}`);

// --------------------------------------------------------------------------- //
// P8-T12/T13 — alerting: channels, rules, events                              //
// --------------------------------------------------------------------------- //
// Secret sub-fields of a channel config are WRITE-ONLY: a GET returns the
// "__redacted__" marker, and an edit that keeps a secret sends the
// "__unchanged__" sentinel (or omits it). The decrypted value never round-trips.
export const CHANNEL_TYPES = ["webhook", "email", "apprise"] as const;
export type ChannelType = (typeof CHANNEL_TYPES)[number];
export const DISPATCH_LOCALITIES = ["central", "agent"] as const;
export type DispatchLocality = (typeof DISPATCH_LOCALITIES)[number];
export const EVENT_TYPES = ["created", "modified", "deleted", "moved"] as const;
export type AlertEventType = (typeof EVENT_TYPES)[number];
export const DIGEST_WINDOWS = ["hourly", "daily"] as const;
export type DigestWindow = (typeof DIGEST_WINDOWS)[number];

// FIX-16: per-channel webhook payload shape. `generic` is Filearr's native
// signed JSON (back-compat default); `discord`/`slack` reshape the body so those
// endpoints accept it (Discord rejects a body without content/embeds).
export const WEBHOOK_FORMATS = ["generic", "discord", "slack"] as const;
export type WebhookFormat = (typeof WEBHOOK_FORMATS)[number];

/** Auto-detect a webhook format from its URL (new-channel UI default; the
 *  select stays editable). Mirrors the backend `webhook_formats.detect_format`. */
export function detectWebhookFormat(url: string): WebhookFormat {
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    const discordHosts = ["discord.com", "discordapp.com"];
    const isDiscordHost =
      discordHosts.includes(host) ||
      discordHosts.some((h) => host.endsWith("." + h));
    if (isDiscordHost && u.pathname.includes("/api/webhooks")) return "discord";
    if (host === "hooks.slack.com") return "slack";
  } catch {
    // not a parseable URL yet — fall through to generic
  }
  return "generic";
}

/** Sentinel a client sends to KEEP an existing (encrypted) channel secret. */
export const UNCHANGED = "__unchanged__";
/** Marker the API returns in place of any stored secret on read. */
export const REDACTED = "__redacted__";

export interface AlertChannel {
  id: string;
  name: string;
  type: ChannelType;
  config: Record<string, unknown>;
  dispatch_locality: DispatchLocality;
  enabled: boolean;
  created_at: string;
}

export const listAlertChannels = () => request<AlertChannel[]>("/alert-channels");

export const createAlertChannel = (body: {
  name: string;
  type: ChannelType;
  config: Record<string, unknown>;
  dispatch_locality?: DispatchLocality;
  enabled?: boolean;
}) => request<AlertChannel>("/alert-channels", { method: "POST", body: JSON.stringify(body) });

export const updateAlertChannel = (
  id: string,
  patch: Partial<{
    name: string;
    config: Record<string, unknown>;
    dispatch_locality: DispatchLocality;
    enabled: boolean;
  }>,
) => request<AlertChannel>(`/alert-channels/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

export async function deleteAlertChannel(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/alert-channels/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

export interface TestFireResult {
  ok: boolean;
  detail: string;
  status_code: number | null;
  retryable: boolean;
}

/** Fire a sample alert through the real driver (delivery failures come back in
 *  the 200 body with ok=false; config/secret problems are 4xx/503). */
export const testAlertChannel = (id: string) =>
  request<TestFireResult>(`/alert-channels/${id}/test`, { method: "POST" });

export interface AlertRule {
  id: string;
  name: string;
  enabled: boolean;
  is_system: boolean;
  library_id: string | null;
  path_glob: string | null;
  event_types: string[];
  hash_change_only: boolean;
  group_by: string[];
  group_wait_s: number;
  digest_window: DigestWindow | null;
  repeat_interval_s: number | null;
  threshold_count: number | null;
  threshold_window_s: number | null;
  channel_ids: string[];
  created_at: string;
}

export const listAlertRules = () => request<AlertRule[]>("/alert-rules");

export const createAlertRule = (body: {
  name: string;
  enabled?: boolean;
  library_id?: string | null;
  path_glob?: string | null;
  event_types: string[];
  hash_change_only?: boolean;
  group_wait_s?: number;
  digest_window?: DigestWindow | null;
  repeat_interval_s?: number | null;
  channel_ids?: string[];
}) => request<AlertRule>("/alert-rules", { method: "POST", body: JSON.stringify(body) });

// System rules: only channels + throttle/timings are editable (the match logic
// is read-only). User rules may patch every field below.
export const updateAlertRule = (
  id: string,
  patch: Partial<{
    name: string;
    enabled: boolean;
    library_id: string | null;
    path_glob: string | null;
    event_types: string[];
    hash_change_only: boolean;
    group_wait_s: number;
    digest_window: DigestWindow | null;
    repeat_interval_s: number | null;
    channel_ids: string[];
  }>,
) => request<AlertRule>(`/alert-rules/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

export async function deleteAlertRule(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/alert-rules/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

export type AlertEventStatus = "delivered" | "failed" | "pending";

export interface AlertEvent {
  id: string;
  rule_id: string;
  item_id: string | null;
  library_id: string | null;
  event_type: string;
  dedup_key: string;
  status: AlertEventStatus;
  delivered: boolean;
  delivered_at: string | null;
  delivery_attempts: number;
  occurred_at: string;
  last_error: string | null;
}

export const listAlertEvents = (
  filters: { rule_id?: string; library_id?: string; status?: AlertEventStatus; limit?: number } = {},
) => {
  const qs = new URLSearchParams();
  if (filters.rule_id) qs.set("rule_id", filters.rule_id);
  if (filters.library_id) qs.set("library_id", filters.library_id);
  if (filters.status) qs.set("status", filters.status);
  qs.set("limit", String(filters.limit ?? 100));
  return request<AlertEvent[]>(`/alert-events?${qs}`);
};

export interface AlertEventSummary {
  delivered: number;
  failed: number;
  pending: number;
}

export const alertEventsSummary = (library_id?: string) =>
  request<AlertEventSummary>(
    `/alert-events/summary${library_id ? `?library_id=${encodeURIComponent(library_id)}` : ""}`,
  );

// ---- P3-T10 duplicate awareness (copy counts + copy listing) ----
export interface ItemCopy {
  id: string;
  library_id: string;
  library_name: string | null;
  rel_path: string;
  path: string;
  native_path: string | null;
  size: number;
  last_seen: string;
}

export interface CopiesResponse {
  id: string;
  /** Full group size INCLUDING this item — the badge reads "N copies". */
  count: number;
  /** Which key grouped the copies: "content_hash" | "quick_hash" | "none". */
  match: string;
  capped: boolean;
  copies: ItemCopy[];
}

/** The OTHER active copies of an item (read scope). Used by the ItemDetail
 *  Copies section; `count` is the full group size incl. self. */
export const itemCopies = (id: string) =>
  request<CopiesResponse>(`/items/${id}/copies`);

/** Batch copy-count badges for a page of results (read scope). Body is up to 200
 *  ids; the response maps id -> count ONLY for groups with more than one member.
 *  A single grouped SQL pass — never a per-row query. */
export const copyCounts = (ids: string[]) =>
  request<Record<string, number>>("/items/copy-counts", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });

// ---- P3-T12 tag facet type-ahead ----
export interface TagSuggestion {
  value: string;
  count: number;
}

/** Typo-tolerant, count-ordered tag suggestions from Meili facet-search (read
 *  scope). `q` is the partial tag; the optional scope narrows the counts. */
export function searchTags(
  q: string,
  scope: { type?: string; library?: string } = {},
  signal?: AbortSignal,
): Promise<{ tags: TagSuggestion[] }> {
  const params: Record<string, string> = { q };
  if (scope.type) params.type = scope.type;
  if (scope.library) params.library = scope.library;
  const qs = new URLSearchParams(params);
  return request(`/search/tags?${qs}`, { signal });
}

// ---- P3-T14 timeline (date histogram over mtime) ----
export interface TimelineBucket {
  start: string;
  start_epoch: number;
  end_epoch: number;
  count: number;
}

export interface TimelineResponse {
  bucket: "month" | "year";
  library: string | null;
  buckets: TimelineBucket[];
  invalid_count: number;
  /** mtime_gte value the UI uses to filter the "invalid dates" (future) bucket. */
  invalid_mtime_gte: number;
}

/** Date histogram of active items by mtime (read scope). `bucket` is month|year;
 *  `library` scopes to one library. */
export const timeline = (bucket: "month" | "year" = "month", library = "") => {
  const qs = new URLSearchParams({ bucket });
  if (library) qs.set("library", library);
  return request<TimelineResponse>(`/stats/timeline?${qs}`);
};

// ---- P11 reporting v1 ----
export type RowLink = "item" | "search_ext" | "search_hash" | "none";

export interface ReportMeta {
  id: string;
  title: string;
  description: string;
  columns: string[];
  supports_library: boolean;
  is_capped: boolean;
  default_limit: number;
  /** How the UI makes a row interactive (P11 polish). */
  row_link: RowLink;
}

/** The streaming machine-readable export formats (JSON stays the paginated
 *  envelope). Drives the Download dropdown + per-format file extension. */
export type ExportFormat = "csv" | "ndjson" | "xml" | "xlsx";
export const EXPORT_FORMATS: ExportFormat[] = ["csv", "ndjson", "xml", "xlsx"];

export interface ReportPage {
  report: ReportMeta;
  columns: string[];
  rows: Record<string, unknown>[];
  limit: number;
  offset: number;
  count: number;
  has_more: boolean;
}

/** The canned-report registry (metadata only). */
export const listReports = () =>
  request<{ reports: ReportMeta[] }>("/reports").then((r) => r.reports);

/** Build the query string shared by the JSON-page and CSV-download paths. */
function reportQuery(opts: { limit?: number; offset?: number; libraryId?: string }): string {
  const qs = new URLSearchParams();
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.offset != null) qs.set("offset", String(opts.offset));
  if (opts.libraryId) qs.set("library_id", opts.libraryId);
  const s = qs.toString();
  return s ? `?${s}` : "";
}

/** Run a canned report and return one JSON page. */
export const runReport = (
  id: string,
  opts: { limit?: number; offset?: number; libraryId?: string } = {},
) => request<ReportPage>(`/reports/${id}${reportQuery(opts)}`);

/** Save a streamed fetch Response body to a download file. Central because a
 *  bare <a download> link can't carry the Bearer auth header, so every export
 *  goes fetch -> blob -> object URL -> synthetic click. */
async function saveBlob(res: Response, filename: string): Promise<void> {
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function stampToday(): string {
  return new Date().toISOString().slice(0, 10).replace(/-/g, "");
}

/** Download a canned report in a chosen streaming format (csv/ndjson/xml). Uses
 *  fetch (auth header) + a blob so the streamed body is saved, not rendered. */
export async function downloadReport(
  id: string,
  format: ExportFormat,
  opts: { limit?: number; libraryId?: string } = {},
): Promise<void> {
  const q = reportQuery({ ...opts, offset: undefined });
  const sep = q ? "&" : "?";
  const res = await fetch(`${API_BASE}/reports/${id}${q}${sep}format=${format}`, {
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  await saveBlob(res, `filearr-${id}-${stampToday()}.${format}`);
}

// --------------------------------------------------------------------------- //
// P11-T5/T9/T11 — background export JOBS + scheduled report delivery.          //
// --------------------------------------------------------------------------- //
export type ExportStatus = "queued" | "running" | "complete" | "failed";

export interface ReportExport {
  id: string;
  status: ExportStatus;
  format: ExportFormat;
  canned_report_key: string | null;
  report_definition_id: string | null;
  triggered_by: string;
  row_count: number | null;
  file_size_bytes: number | null;
  error: string | null;
  delivery_status: string | null;
  created_at: string | null;
  finished_at: string | null;
  expires_at: string | null;
  purged_at: string | null;
  downloadable: boolean;
}

/** Queue a background export of a canned report. */
export const enqueueReportExport = (
  id: string,
  format: ExportFormat,
  opts: { limit?: number; libraryId?: string } = {},
) => {
  const qs = new URLSearchParams({ format });
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.libraryId) qs.set("library_id", opts.libraryId);
  return request<ReportExport>(`/reports/${id}/export?${qs.toString()}`, {
    method: "POST",
  });
};

/** Queue a background export of a custom report. */
export const enqueueCustomReportExport = (
  id: string,
  format: ExportFormat,
  opts: { limit?: number } = {},
) => {
  const qs = new URLSearchParams({ format });
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  return request<ReportExport>(`/custom-reports/${id}/export?${qs.toString()}`, {
    method: "POST",
  });
};

export const listExports = () =>
  request<{ exports: ReportExport[] }>("/exports").then((r) => r.exports);

export const getExport = (id: string) => request<ReportExport>(`/exports/${id}`);

/** Fetch a finished export artifact (auth header) and save it as a file. */
export async function downloadExport(ex: ReportExport): Promise<void> {
  const res = await fetch(`${API_BASE}/exports/${ex.id}/download`, {
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  await saveBlob(res, `filearr-export-${ex.id}.${ex.format}`);
}

// ---- Scheduled reports (P11-T9) ----
export interface ReportSchedule {
  id: string;
  name: string;
  owner_principal: string | null;
  canned_report_key: string | null;
  report_definition_id: string | null;
  params: Record<string, unknown>;
  format: ExportFormat;
  cron: string;
  channel_id: string | null;
  enabled: boolean;
  last_cron_fired_at: string | null;
  created_at: string;
  updated_at: string;
}

export const listReportSchedules = () =>
  request<ReportSchedule[]>("/report-schedules");

export const createReportSchedule = (body: {
  name: string;
  canned_report_key?: string | null;
  report_definition_id?: string | null;
  params?: Record<string, unknown>;
  format: ExportFormat;
  cron: string;
  channel_id?: string | null;
  enabled?: boolean;
}) =>
  request<ReportSchedule>("/report-schedules", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateReportSchedule = (
  id: string,
  patch: Partial<{
    name: string;
    params: Record<string, unknown>;
    format: ExportFormat;
    cron: string;
    channel_id: string | null;
    enabled: boolean;
  }>,
) =>
  request<ReportSchedule>(`/report-schedules/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });

export async function deleteReportSchedule(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/report-schedules/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

// --------------------------------------------------------------------------- //
// Phase 6 (P6-T1) — local accounts + sessions.                                //
// The session cookie (filearr_session, HttpOnly) rides along automatically on  //
// same-origin fetches; these helpers never touch the token. `authStatus` is    //
// the public probe that tells the SPA whether to show a login wall.            //
// --------------------------------------------------------------------------- //
export type AuthMode = "disabled" | "bootstrap" | "enabled";

export interface AuthStatus {
  auth_enabled: boolean;
  users_exist: boolean;
  mode: AuthMode;
  // P6-T5: when true the login page shows a "Sign in with SSO" button.
  oidc_enabled: boolean;
}

/** Full-page navigation target that starts the OIDC redirect flow. This is a
 *  top-level browser navigation (NOT a fetch) — the IdP round-trip and the
 *  Set-Cookie on return require a real navigation. ``returnTo`` (a local path)
 *  is where the callback sends the browser after a successful login. */
export function oidcLoginUrl(returnTo = "/"): string {
  const q = new URLSearchParams({ return_to: returnTo }).toString();
  return `${API_BASE}/auth/oidc/login?${q}`;
}

export interface AuthPrincipal {
  id: string;
  username: string;
  email: string | null;
  global_role: "admin" | "user" | "viewer";
  kind: string;
  disabled: boolean;
  // P6-T10/T12: identity source ('local'|'ldap'|'saml'|'oidc'). Older payloads
  // may omit it (defaults 'local' server-side).
  auth_provider?: string;
}

export interface LoginResult {
  principal: AuthPrincipal;
  warning: string | null;
}

export const authStatus = () => request<AuthStatus>("/auth/status");

/** Current session principal, or null when not authenticated (401). */
export async function authMe(): Promise<AuthPrincipal | null> {
  const res = await fetch(`${API_BASE}/auth/me`, { credentials: "same-origin" });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json() as Promise<AuthPrincipal>;
}

export function authLogin(username: string, password: string): Promise<LoginResult> {
  return request<LoginResult>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function authBootstrap(username: string, password: string): Promise<AuthPrincipal> {
  return request<AuthPrincipal>("/auth/bootstrap", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export async function authLogout(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, { method: "POST", credentials: "same-origin" });
}

// --------------------------------------------------------------------------- //
// P6-T2 — RBAC: users, groups, path grants, decision preview (admin scope).   //
// --------------------------------------------------------------------------- //
export interface RbacGroup {
  id: string;
  name: string;
  description: string | null;
  source: string;
  member_count: number;
}
export interface RbacMember {
  principal_id: string;
  username: string | null;
  global_role: string | null;
}
export interface RbacGrant {
  id: string;
  subject_kind: "principal" | "group";
  subject_id: string;
  subject_label: string | null;
  library_id: string;
  scope: string;
  action: string;
  effect: "allow" | "deny";
}
export interface RbacActions {
  actions: string[];
  role_ceilings: Record<string, string[]>;
}
export interface RbacDecision {
  allowed: boolean;
  reason: string;
  action: string;
  role: string;
  item_scope: string;
  winning_grant: {
    scope: string;
    action: string;
    effect: string;
    subject_kind: string | null;
    subject_id: string | null;
  } | null;
}

export const listRbacActions = () => request<RbacActions>("/rbac/actions");
export const listUsers = () => request<AuthPrincipal[]>("/auth/users");

export const listGroups = () => request<RbacGroup[]>("/rbac/groups");
export const createGroup = (name: string, description: string | null) =>
  request<RbacGroup>("/rbac/groups", {
    method: "POST",
    body: JSON.stringify({ name, description }),
  });
export async function deleteGroup(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/rbac/groups/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
}
export const listMembers = (groupId: string) =>
  request<RbacMember[]>(`/rbac/groups/${groupId}/members`);
export async function addMember(groupId: string, principalId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/rbac/groups/${groupId}/members`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}),
    },
    body: JSON.stringify({ principal_id: principalId }),
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
}
export async function removeMember(groupId: string, principalId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/rbac/groups/${groupId}/members/${principalId}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
}

export const listGrants = () => request<RbacGrant[]>("/rbac/grants");
export const createGrant = (body: {
  subject_kind: "principal" | "group";
  subject_id: string;
  library_id: string;
  rel_path: string;
  action: string;
  effect: "allow" | "deny";
}) =>
  request<RbacGrant>("/rbac/grants", {
    method: "POST",
    body: JSON.stringify(body),
  });
export async function deleteGrant(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/rbac/grants/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
}

export const rbacPreview = (
  principal: string,
  library: string,
  path: string,
  action: string,
) =>
  request<RbacDecision>(
    `/rbac/preview?principal=${encodeURIComponent(principal)}` +
      `&library=${encodeURIComponent(library)}` +
      `&path=${encodeURIComponent(path)}` +
      `&action=${encodeURIComponent(action)}`,
  );

// ---- P11 custom (saved-query) reports ----
export interface ReportDefinition {
  id: string;
  name: string;
  owner_principal: string | null;
  query: string;
  columns: string[];
  sort: string | null;
  format: string;
  created_at: string;
  updated_at: string;
}

export interface ReportValidationError {
  error: string;
  code?: string;
  position?: number;
  reason?: string;
  message?: string;
  unsupported?: string[];
}

export interface CustomRunPage {
  report: { id: string; name: string; columns: string[] };
  columns: string[];
  rows: Record<string, unknown>[];
  limit: number;
  offset: number;
  count: number;
  has_more: boolean;
}

export interface ColumnRegistry {
  core: string[];
  custom_fields: string[];
  formats: string[];
}

export const listCustomReports = () =>
  request<ReportDefinition[]>("/custom-reports");

export const getColumnRegistry = () =>
  request<ColumnRegistry>("/custom-reports/columns");

export const validateCustomReport = (body: {
  query: string;
  columns: string[];
  sort?: string | null;
}) =>
  request<{ ok: boolean; errors: ReportValidationError[] }>(
    "/custom-reports/validate",
    { method: "POST", body: JSON.stringify(body) },
  );

export const createCustomReport = (body: {
  name: string;
  query: string;
  columns: string[];
  sort?: string | null;
  format?: string;
}) =>
  request<ReportDefinition>("/custom-reports", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateCustomReport = (
  id: string,
  body: Partial<{
    name: string;
    query: string;
    columns: string[];
    sort: string | null;
    format: string;
  }>,
) =>
  request<ReportDefinition>(`/custom-reports/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export async function deleteCustomReport(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/custom-reports/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
}

export const runCustomReport = (
  id: string,
  opts: { limit?: number; offset?: number } = {},
) => {
  const qs = new URLSearchParams({ format: "json" });
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.offset != null) qs.set("offset", String(opts.offset));
  return request<CustomRunPage>(`/custom-reports/${id}/run?${qs.toString()}`);
};

/** Download a custom report in a chosen streaming format (csv/ndjson/xml). */
export async function downloadCustomReport(
  id: string,
  name: string,
  format: ExportFormat,
): Promise<void> {
  const res = await fetch(`${API_BASE}/custom-reports/${id}/run?format=${format}`, {
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  // The server sets a safe Content-Disposition filename; the client name is only
  // a friendly default (sanitised for the filesystem).
  const safe = name.replace(/[^\w.-]+/g, "_") || "report";
  await saveBlob(res, `filearr-${safe}-${stampToday()}.${format}`);
}

// --------------------------------------------------------------------------- //
// P6-T8/T9/T11/T12 — security hardening: users, sessions, audit.              //
// --------------------------------------------------------------------------- //

// ---- User management (admin) ----
export const createUser = (body: {
  username: string;
  password: string;
  global_role?: "admin" | "user" | "viewer";
  email?: string | null;
}) => request<AuthPrincipal>("/auth/users", { method: "POST", body: JSON.stringify(body) });

export const updateUser = (
  id: string,
  patch: Partial<{
    global_role: "admin" | "user" | "viewer";
    disabled: boolean;
    email: string | null;
    password: string;
  }>,
) => request<AuthPrincipal>(`/auth/users/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

export async function deleteUser(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/users/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
    credentials: "same-origin",
  });
  if (res.status === 204) return;
  throw new ApiError(res.status, await res.text());
}

// ---- Active sessions (P6-T11) ----
export interface AuthSession {
  id: string;
  ip_address: string | null;
  user_agent: string | null;
  created_at: string;
  last_seen_at: string;
  current: boolean;
}

export const listMySessions = () => request<AuthSession[]>("/auth/sessions");

export async function revokeMySession(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/sessions/${id}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new ApiError(res.status, await res.text());
}

export async function revokeAllMySessions(): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/sessions/revoke-all`, {
    method: "POST",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new ApiError(res.status, await res.text());
}

export const listUserSessions = (principalId: string) =>
  request<AuthSession[]>(`/auth/users/${principalId}/sessions`);

export async function revokeUserSessions(principalId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/users/${principalId}/sessions`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new ApiError(res.status, await res.text());
}

// ---- Security audit feed (P6-T9, admin) ----
export interface SecurityEvent {
  id: string;
  event_type: string;
  principal_id: string | null;
  username_attempted: string | null;
  ip: string | null;
  user_agent: string | null;
  ts: string;
  details: Record<string, unknown> | null;
}

export interface AuditPage {
  events: SecurityEvent[];
  next_cursor: string | null;
}

export const listAudit = (
  filters: {
    event_type?: string;
    principal_id?: string;
    since?: string;
    until?: string;
    cursor?: string;
    limit?: number;
  } = {},
) => {
  const qs = new URLSearchParams();
  if (filters.event_type) qs.set("event_type", filters.event_type);
  if (filters.principal_id) qs.set("principal_id", filters.principal_id);
  if (filters.since) qs.set("since", filters.since);
  if (filters.until) qs.set("until", filters.until);
  if (filters.cursor) qs.set("cursor", filters.cursor);
  qs.set("limit", String(filters.limit ?? 50));
  return request<AuditPage>(`/audit?${qs}`);
};

// --------------------------------------------------------------------------- //
// Visual filter builder (user-requested) — live preview + key vocabulary.     //
// --------------------------------------------------------------------------- //
export interface QueryPreviewResponse {
  columns: string[];
  rows: Record<string, unknown>[];
  limit: number;
  offset: number;
  count: number;
  has_more: boolean;
  /** Match count, capped at the server ceiling (10k). */
  total: number;
  /** True when the real match count exceeds the ceiling (render "total+"). */
  total_capped: boolean;
}

/** Reuses the SAME structured validation-error shape as custom reports. */
export type QueryPreviewError = ReportValidationError;

export interface MetaKeyInfo {
  key: string;
  label: string;
  data_type: string;
  media_types: string[];
}

export interface CustomFieldKeyInfo {
  name: string;
  label: string;
  data_type: string;
  select_options: string[] | null;
}

export interface QueryKeys {
  meta_keys: MetaKeyInfo[];
  custom_fields: CustomFieldKeyInfo[];
  kinds: string[];
  source: string;
}

/** Live-preview a querydsl string against real data (read scope, RBAC-scoped).
 *  On a parse/translation error the server returns 422 with the same structured
 *  `{ detail: { validation: [...] } }` body the reports validate/run paths use. */
export async function previewQuery(
  body: { query: string; limit?: number; offset?: number },
  signal?: AbortSignal,
): Promise<QueryPreviewResponse> {
  const res = await fetch(`${API_BASE}/query/preview`, {
    method: "POST",
    signal,
    headers: {
      "Content-Type": "application/json",
      ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json() as Promise<QueryPreviewResponse>;
}

/** Extract the structured validation errors from a failed previewQuery (422). */
export function previewValidationErrors(e: unknown): QueryPreviewError[] | null {
  if (!(e instanceof ApiError) || e.status !== 422) return null;
  try {
    const body = JSON.parse(e.body);
    const v = body?.detail?.validation;
    return Array.isArray(v) ? (v as QueryPreviewError[]) : null;
  } catch {
    return null;
  }
}

export const queryKeys = () => request<QueryKeys>("/query/keys");


// --------------------------------------------------------------------------- //
// P5-T1 — distributed-agent enrollment (admin scope). Mint single-use tokens,  //
// list/revoke tokens + agents. The raw token is shown ONCE by the mint call.   //
// --------------------------------------------------------------------------- //
export type AgentStatus = "pending" | "active" | "revoked";

export interface EnrollmentTokenOut {
  token_hash: string;
  rollout_group: string;
  expires_at: string;
  consumed_at: string | null;
  consumed_by: string | null;
  created_at: string;
  status: "active" | "consumed" | "expired";
}

export interface EnrollmentTokenMint extends EnrollmentTokenOut {
  token: string; // raw, show-once
}

export interface AgentOut {
  id: string;
  name: string;
  hostname: string;
  platform: string;
  rollout_group: string;
  status: AgentStatus;
  cert_fingerprint: string | null;
  last_contiguous_seq_no: number;
  last_seen_at: string | null;
  agent_version: string | null;
  policy_version_applied: number | null;
  revoked_at: string | null;
  created_at: string;
  // W6-D4: current config-group assignment (null = built-in defaults).
  config_group_id: string | null;
}

export const listAgents = () => request<AgentOut[]>("/agents");

export const listEnrollmentTokens = () =>
  request<EnrollmentTokenOut[]>("/agents/enrollment-tokens");

export const mintEnrollmentToken = (rollout_group: string, ttl_minutes?: number) =>
  request<{
    token: string;
    token_hash: string;
    rollout_group: string;
    expires_at: string;
  }>("/agents/enrollment-tokens", {
    method: "POST",
    body: JSON.stringify({ rollout_group, ...(ttl_minutes ? { ttl_minutes } : {}) }),
  });

/** Delete an enrollment token. Unconsumed tokens delete freely; a consumed
 *  token's row (which carries the consumed_by link) needs `force` — the audit
 *  event preserves the link before the row goes. */
export async function revokeEnrollmentToken(tokenHash: string, force = false): Promise<void> {
  const res = await fetch(
    `${API_BASE}/agents/enrollment-tokens/${encodeURIComponent(tokenHash)}${force ? "?force=true" : ""}`,
    {
      method: "DELETE",
      credentials: "same-origin",
      headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
    },
  );
  if (!res.ok && res.status !== 204) throw new ApiError(res.status, await res.text());
}

/** Revoke = application-layer denylist (row retained, history kept). */
export const revokeAgent = (id: string) =>
  request<AgentOut>(`/agents/${id}`, { method: "DELETE" });

/** HARD delete an agent row — the cleanup path for failed enrollments and
 *  data-free decommissions. 409 while any library/item references the agent. */
export const deleteAgent = (id: string) =>
  request<AgentOut>(`/agents/${id}?purge=true`, { method: "DELETE" });

// --------------------------------------------------------------------------- //
// P10-T1 — agent_commands (on-demand command primitive). Admin/read surface:   //
// list an agent's commands + cancel a pre-terminal one. Enqueue + the agent    //
// plane (poll/ack/complete) are driven by the agent runtime / retrieve flow.   //
// --------------------------------------------------------------------------- //
export type AgentCommandStatus =
  | "pending"
  | "picked_up"
  | "done"
  | "failed"
  | "expired"
  | "cancelled";
export type AgentCommandKind = "stat_check" | "rehash_check" | "stage_upload";

export interface AgentCommandOut {
  id: string;
  agent_id: string;
  kind: AgentCommandKind;
  item_id: string;
  payload: Record<string, unknown>;
  status: AgentCommandStatus;
  attempts: number;
  created_at: string;
  updated_at: string;
  expires_at: string;
  picked_up_at: string | null;
  completed_at: string | null;
  result: Record<string, unknown> | null;
  requested_by: string | null;
}

export const AGENT_COMMAND_TERMINAL: AgentCommandStatus[] = [
  "done",
  "failed",
  "expired",
  "cancelled",
];

export const listAgentCommands = (agentId?: string, limit = 50) =>
  request<AgentCommandOut[]>(
    `/agent-commands?${new URLSearchParams({
      ...(agentId ? { agent_id: agentId } : {}),
      limit: String(limit),
    })}`,
  );

export const cancelAgentCommand = (id: string) =>
  request<AgentCommandOut>(`/agent-commands/${id}/cancel`, { method: "POST" });

// --------------------------------------------------------------------------- //
// P10-T12 — central agent share-maps (admin CRUD, user-mandated). Define how a  //
// path on an agent maps to a network share so an agent-hosted file still gets a  //
// network-open link when the agent can't self-report one (P10-T11). Longest-     //
// local_prefix-wins resolution; an agent-scoped rule outranks a global one.      //
// All behind the agents feature gate; mutations require admin scope.             //
// --------------------------------------------------------------------------- //
export interface ShareLocationOut {
  url: string | null;
  unc: string | null;
}

export interface AgentShareMapOut {
  id: string;
  agent_id: string | null; // null = any agent (global fallback)
  library_id: string | null;
  local_prefix: string;
  share_prefix: string;
  unc: string | null;
  storage_type: string | null;
  host: string | null;
  created_at: string;
  updated_at: string;
  location: ShareLocationOut; // both-format preview of local_prefix itself
}

export interface AgentShareMapCreate {
  library_id?: string | null;
  local_prefix: string;
  share_prefix: string;
  unc?: string | null;
  storage_type?: string | null;
  host?: string | null;
}

export const listAgentShareMaps = (agentId?: string) =>
  request<AgentShareMapOut[]>(
    `/agent-share-maps?${new URLSearchParams(agentId ? { agent_id: agentId } : {})}`,
  );

export const createAgentShareMap = (agentId: string, body: AgentShareMapCreate) =>
  request<AgentShareMapOut>(`/agents/${agentId}/share-maps`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateAgentShareMap = (
  id: string,
  patch: Partial<AgentShareMapCreate>,
) =>
  request<AgentShareMapOut>(`/agent-share-maps/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });

export async function deleteAgentShareMap(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/agent-share-maps/${id}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok && res.status !== 204) throw new ApiError(res.status, await res.text());
}

// --------------------------------------------------------------------------- //
// W6-D4 — agent management page: fleet summary tallies, config-group CRUD +     //
// assignment, and console installer distribution. Types mirror the W6-D2 frozen //
// backend shapes (filearr.agent_config.GroupSettings + the installer contract). //
// --------------------------------------------------------------------------- //

/** GET /agents/summary — status-header counts (read scope). connected +
 *  disconnected = active (cert-bound) agents split by liveness; pending/revoked
 *  are lifecycle buckets; total is the sum of all four. */
export interface AgentFleetSummary {
  total: number;
  connected: number;
  disconnected: number;
  pending: number;
  revoked: number;
}

export const getAgentSummary = () =>
  request<AgentFleetSummary>("/agents/summary");

/** The named folder-selection presets a config group's scan_selections may use
 *  (mirrors filearr.agent_config.SCAN_PRESET_NAMES). "custom" is the empty,
 *  admin-defined scaffold. */
export const SCAN_PRESET_NAMES = [
  "user-documents",
  "user-media",
  "user-profiles-full",
  "downloads",
  "server-data",
  "custom",
] as const;
export type ScanPresetName = (typeof SCAN_PRESET_NAMES)[number];

/** Config-group log levels (mirrors filearr.agent_config.LOG_LEVELS). */
export const AGENT_LOG_LEVELS = ["error", "warn", "info", "verbose", "debug"] as const;
export type AgentLogLevel = (typeof AGENT_LOG_LEVELS)[number];

/** One path selection an agent walks. Either a preset OR explicit path specs
 *  (or both); include/exclude regexes refine matches; enabled gates it. */
export interface ScanSelection {
  preset?: string | null;
  paths?: string[];
  include_regex?: string[];
  exclude_regex?: string[];
  enabled?: boolean;
}

export interface InventoryConfig {
  enabled?: boolean;
  collectors?: string[];
}

/** The typed config-group settings object (v1). Unknown top-level keys are
 *  REJECTED by the backend (422) — keep this in lockstep with GroupSettings. */
export interface GroupSettings {
  log_level?: AgentLogLevel | null;
  scan_selections?: ScanSelection[] | null;
  inventory?: InventoryConfig | null;
  scan_schedule_cron?: string | null;
}

export interface ConfigGroupOut {
  id: string;
  name: string;
  description: string | null;
  settings: GroupSettings;
  member_count: number;
  created_at: string;
  updated_at: string;
}

export interface ConfigGroupIn {
  name: string;
  description?: string | null;
  settings?: GroupSettings;
}

export interface ConfigGroupUpdateIn {
  name?: string;
  description?: string | null;
  settings?: GroupSettings;
}

export const listConfigGroups = () =>
  request<ConfigGroupOut[]>("/agents/config-groups");

export const createConfigGroup = (body: ConfigGroupIn) =>
  request<ConfigGroupOut>("/agents/config-groups", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateConfigGroup = (id: string, patch: ConfigGroupUpdateIn) =>
  request<ConfigGroupOut>(`/agents/config-groups/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });

export async function deleteConfigGroup(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/agents/config-groups/${id}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok && res.status !== 204) throw new ApiError(res.status, await res.text());
}

/** PUT /agents/{id}/config-group — assign (or clear with null). Returns the
 *  newly-assigned group, or null when cleared. */
export const assignConfigGroup = (agentId: string, groupId: string | null) =>
  request<ConfigGroupOut | null>(`/agents/${agentId}/config-group`, {
    method: "PUT",
    body: JSON.stringify({ config_group_id: groupId }),
  });

// ---- console installer distribution (POST /agents/installer-config) ----------
export interface InstallerConfigIn {
  central_url_override?: string | null;
  agent_name?: string | null;
  config_group_id?: string | null;
  log_level?: string | null;
  ttl_seconds?: number | null;
}

export interface InstallerSidecar {
  central_url: string;
  enrollment_token: string; // raw, show-once
  agent_name: string | null;
  config_group: string | null; // group NAME
  log_level: string | null;
}

export interface InstallHint {
  windows: string;
  linux: string;
  macos: string;
}

export interface InstallerConfigOut {
  sidecar: InstallerSidecar;
  token_hash: string;
  expires_at: string;
  install_hint: InstallHint;
}

export const issueInstallerConfig = (body: InstallerConfigIn) =>
  request<InstallerConfigOut>("/agents/installer-config", {
    method: "POST",
    body: JSON.stringify(body),
  });
