// P3-T7 — deep-linkable search state <-> URL hash query.
//
// The SearchPage reflects its current flat /search params into the location hash
// as `#/search?<querystring>` so a search is bookmarkable / shareable and
// survives a refresh, and so browser back/forward walks the search history. This
// module is PURE (no DOM, no Svelte) so it is trivially testable and reused by
// both the reflect (write) and restore (read) paths.
//
// The param vocabulary is intentionally the SAME flat record the backend accepts
// (type/q/extension/size_gte/…): a saved search stores exactly this record, and
// replaying it (apply -> buildParams -> /search) hits the engine identically.

export const SEARCH_ROUTE = "#/search";

/** Serialise a flat params record into a `#/search?...` hash.
 *  Empty / null values are dropped; keys are sorted for a stable, diff-friendly,
 *  deep-linkable URL (two equal states always encode to the same string). */
export function encodeSearchHash(params: Record<string, string>): string {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== "" && v != null),
  );
  qs.sort();
  const s = qs.toString();
  return s ? `${SEARCH_ROUTE}?${s}` : SEARCH_ROUTE;
}

/** True when `hash` addresses the search route (with or without a query). The
 *  bare/empty hash and `#/` count as search (the app's default route). */
export function isSearchHash(hash: string): boolean {
  const qi = hash.indexOf("?");
  const prefix = qi === -1 ? hash : hash.slice(0, qi);
  return (
    prefix === SEARCH_ROUTE || prefix === "" || prefix === "#" || prefix === "#/"
  );
}

/** Parse the query part of a `#/search?...` hash back into a flat params record.
 *  Returns `{}` for a non-search hash or a search hash with no query. */
export function parseSearchHash(hash: string): Record<string, string> {
  if (!isSearchHash(hash)) return {};
  const qi = hash.indexOf("?");
  if (qi === -1) return {};
  const out: Record<string, string> = {};
  for (const [k, v] of new URLSearchParams(hash.slice(qi + 1))) out[k] = v;
  return out;
}
