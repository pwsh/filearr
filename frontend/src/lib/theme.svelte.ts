// Svelte 5 runes-based theme store: dark/light/system + custom accent.
type Mode = "light" | "dark" | "system";

const stored = (localStorage.getItem("theme") as Mode) ?? "system";

export const theme = $state({ mode: stored, accent: localStorage.getItem("accent") ?? "#6366f1" });

export function applyTheme() {
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const dark = theme.mode === "dark" || (theme.mode === "system" && prefersDark);
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.style.setProperty("--accent", theme.accent);
  localStorage.setItem("theme", theme.mode);
  localStorage.setItem("accent", theme.accent);
}
