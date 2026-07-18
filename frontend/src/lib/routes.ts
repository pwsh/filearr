// UI-T12 — hash-route helpers for the in-page browse view. The browse route is
// `#/browse/<library_id>/<encoded rel path>` — the rel_path is URL-encoded as a
// SINGLE token (its own '/'s become %2F) so it never collides with the route's
// own path separators. Empty path = the library root.
export function browseHash(libraryId: string, path = ""): string {
  return `#/browse/${encodeURIComponent(libraryId)}/${encodeURIComponent(path)}`;
}

/** Parsed browse route, or null when the hash is not a browse route. */
export function parseBrowseHash(hash: string): { libraryId: string; path: string } | null {
  const prefix = "#/browse/";
  if (!hash.startsWith(prefix)) return null;
  const rest = hash.slice(prefix.length);
  const slash = rest.indexOf("/");
  const libRaw = slash === -1 ? rest : rest.slice(0, slash);
  const pathRaw = slash === -1 ? "" : rest.slice(slash + 1);
  let libraryId = "";
  let path = "";
  try {
    libraryId = decodeURIComponent(libRaw);
  } catch {
    libraryId = libRaw;
  }
  try {
    path = decodeURIComponent(pathRaw);
  } catch {
    path = "";
  }
  return { libraryId, path };
}

/** Navigate the app to the browse view for a folder (drives the hash router). */
export function gotoBrowse(libraryId: string, path = ""): void {
  location.hash = browseHash(libraryId, path);
}
