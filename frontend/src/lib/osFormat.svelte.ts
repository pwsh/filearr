// UI-T15 — reactive, localStorage-persisted share-format preference. Split from
// the pure ./osFormat.ts (which stays runes-free so `node --test` can import it).
import { currentPlatform, type FormatPref, type Platform } from "./osFormat";

const VALID: readonly FormatPref[] = ["auto", "url", "unc"];
const raw = localStorage.getItem("shareFormat") as FormatPref | null;
const initial: FormatPref = raw && VALID.includes(raw) ? raw : "auto";

/** The viewer's detected OS, resolved once at load (does not change mid-session). */
export const detectedPlatform: Platform = currentPlatform();

/** Reactive preference store. Read `shareFormat.pref` in a $derived to re-format
 *  every open/copy affordance the instant the user flips the selector. */
export const shareFormat = $state<{ pref: FormatPref }>({ pref: initial });

/** Set + persist the preference. */
export function setShareFormat(pref: FormatPref): void {
  shareFormat.pref = pref;
  try {
    localStorage.setItem("shareFormat", pref);
  } catch {
    /* private-mode / disabled storage — the in-memory value still applies */
  }
}
