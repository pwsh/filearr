// Small display helpers shared by the per-type detail cards (P4-T10). Pure.

/** Human duration from a seconds value; null when absent/non-positive. */
export function fmtDuration(sec: unknown): string | null {
  const n = typeof sec === "number" ? sec : Number(sec);
  if (!Number.isFinite(n) || n <= 0) return null;
  const s = Math.round(n);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${ss}s`;
  return `${ss}s`;
}

/** Human byte size; null when absent/non-positive. */
export function fmtBytes(b: unknown): string | null {
  const n = typeof b === "number" ? b : Number(b);
  if (!Number.isFinite(n) || n <= 0) return null;
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

/** Coerce an arbitrary metadata value to a short display string, or null. */
export function asStr(v: unknown): string | null {
  if (v == null) return null;
  if (typeof v === "string") return v.trim() ? v : null;
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : null;
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}

/** An array of plain objects (rows for a TrackTable), or null. */
export function asRows(v: unknown): Record<string, unknown>[] | null {
  if (!Array.isArray(v) || v.length === 0) return null;
  const rows = v.filter((x) => x != null && typeof x === "object" && !Array.isArray(x));
  return rows.length ? (rows as Record<string, unknown>[]) : null;
}
