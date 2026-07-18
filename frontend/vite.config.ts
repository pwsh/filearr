import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// Vite 8: Rolldown-based production builds by default.
//
// AGPL-3.0 §13: the running instance must offer users its Corresponding Source.
// The footer "Source" link points here. Override at build time with
// FILEARR_SOURCE_URL (e.g. a fork/tagged release) — defaults to the canonical
// repository.
const SOURCE_URL =
  process.env.FILEARR_SOURCE_URL ?? "https://github.com/filearr/filearr";

export default defineConfig({
  define: {
    __SOURCE_URL__: JSON.stringify(SOURCE_URL),
    __APP_VERSION__: JSON.stringify(process.env.FILEARR_VERSION ?? "dev"),
  },
  plugins: [
    svelte(),
    tailwindcss(),
    VitePWA({
      registerType: "autoUpdate",
      manifest: {
        name: "Filearr",
        short_name: "Filearr",
        description: "Unified media catalog & search",
        theme_color: "#0f172a",
        display: "standalone",
        icons: [
          { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
          {
            src: "/icon-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
    }),
  ],
  server: {
    proxy: { "/api": "http://localhost:8000" },
  },
});
