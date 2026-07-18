/** Copy text to the clipboard, working on PLAIN-HTTP deployments too.
 *
 * `navigator.clipboard` exists only in secure contexts (https/localhost) —
 * self-hosted Filearr is typically reached over http://<lan-ip>, where the
 * modern API is undefined or rejects ("clipboard blocked", live user report
 * 2026-07-12). Fallback: a temporary off-screen textarea + execCommand("copy"),
 * which still works everywhere for user-gesture-driven copies.
 * Returns true when the text was (apparently) copied.
 */
export async function copyText(text: string): Promise<boolean> {
  if (typeof navigator !== "undefined" && navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to the legacy path (permissions can reject even here)
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
