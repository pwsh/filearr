// UI-T15 — OS-aware network-path formatting. A single network location has two
// OS-native spellings: a URL scheme (`smb://host/share/sub`, what Linux/macOS
// file managers open) and a Windows UNC (`\\host\share\sub`). `smb://` works on
// Linux/mac but NOT in Windows Explorer, and a bare UNC is meaningless on Linux
// — so the UI renders whichever the VIEWER's OS wants (detected, with a manual
// override), and the API serves both so a calling system can pick.
//
// This module is PURE + DOM-free (aside from the tiny navigator reader), so the
// format decision is unit-testable under `node --test` (see tests/osFormat.node.test.ts).
// The reactive, localStorage-persisted preference lives in ./osFormat.svelte.ts.

export type Platform = "windows" | "mac" | "linux" | "other";

/** User preference for which spelling to present. `auto` follows the detected OS. */
export type FormatPref = "auto" | "url" | "unc";

/** Both OS renderings of one network location. Either side may be null. */
export interface ShareLocation {
  url: string | null;
  unc: string | null;
}

/** Classify a raw platform string (navigator.platform / userAgentData.platform). */
export function detectPlatform(raw: string | null | undefined): Platform {
  const p = (raw ?? "").toLowerCase();
  if (!p) return "other";
  if (p.includes("win")) return "windows";
  if (p.includes("mac") || p.includes("iphone") || p.includes("ipad") || p.includes("ios"))
    return "mac";
  if (p.includes("linux") || p.includes("android") || p.includes("x11")) return "linux";
  return "other";
}

/** Read + classify the current platform from the browser. `other` when unknown. */
export function currentPlatform(): Platform {
  try {
    const nav = globalThis.navigator as
      | { userAgentData?: { platform?: string }; platform?: string }
      | undefined;
    const raw = nav?.userAgentData?.platform ?? nav?.platform ?? "";
    return detectPlatform(raw);
  } catch {
    return "other";
  }
}

/** The concrete spelling to use given a preference + platform. `auto` → Windows
 *  gets UNC, everything else gets the URL form. */
export function effectiveFormat(pref: FormatPref, platform: Platform): "url" | "unc" {
  if (pref === "url" || pref === "unc") return pref;
  return platform === "windows" ? "unc" : "url";
}

/** Derive an `smb://` URL from a Windows UNC (`\\host\share\sub` →
 *  `smb://host/share/sub`). Null when the input is not a `\\`-anchored UNC. A
 *  Windows `dashed.ipv6-literal.net` host is restored to a bracketed IPv6 literal.
 *  Mirror of the backend `share_map._derive_url_from_unc`. */
export function deriveSmbFromUnc(unc: string | null | undefined): string | null {
  if (!unc || !unc.startsWith("\\\\")) return null;
  const segs = unc.slice(2).replace(/\\/g, "/").split("/").filter(Boolean);
  if (segs.length === 0) return null;
  let host = segs[0];
  if (host.toLowerCase().endsWith(".ipv6-literal.net")) {
    host = "[" + host.slice(0, -".ipv6-literal.net".length).replace(/-/g, ":") + "]";
  }
  const body = segs.slice(1).join("/");
  return body ? `smb://${host}/${body}` : `smb://${host}`;
}

/** Build a ShareLocation from the backend's effective (url-ish, unc) pair. When
 *  the url-ish value is ITSELF a UNC (a manual UNC share prefix), derive the
 *  `smb://` URL so a non-Windows viewer still gets an openable form, and adopt
 *  the raw UNC as the unc side if the server did not supply one. */
export function shareLocation(
  prefix: string | null | undefined,
  unc: string | null | undefined,
): ShareLocation {
  const p = prefix ?? null;
  let url = p;
  let u = unc ?? null;
  // A UNC prefix (\\host\share) has no URL scheme; derive the smb:// form so a
  // non-Windows viewer still gets something openable.
  if (p && p.startsWith("\\\\")) {
    url = deriveSmbFromUnc(p);
    if (!u) u = p;
  }
  return { url, unc: u };
}

/** Pick the openable prefix string for a location under a preference + platform.
 *  Graceful: if the wanted spelling is missing (e.g. UNC requested for an sftp://
 *  or POSIX location that has none), fall back to the other side. Null when the
 *  location is empty. */
export function formatShare(
  loc: ShareLocation | null | undefined,
  pref: FormatPref,
  platform: Platform,
): string | null {
  if (!loc) return null;
  const want = effectiveFormat(pref, platform);
  if (want === "unc") return loc.unc ?? loc.url ?? null;
  return loc.url ?? loc.unc ?? null;
}
