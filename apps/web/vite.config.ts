import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

// Which build this is: the commit's tree on Railway (GIT_TREE is set for every deploy), a
// timestamp otherwise. Baked into the bundle and written to /version.json, so long-running
// screens can tell a newer build has been deployed (src/lib/version.ts).
const BUILD_ID = process.env.GIT_TREE && process.env.GIT_TREE !== "unknown" ? process.env.GIT_TREE : `local-${Date.now()}`;

function versionFile(): Plugin {
  return {
    name: "wassup-version-file",
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "version.json", source: JSON.stringify({ build: BUILD_ID }) });
    },
  };
}

export default defineConfig({
  plugins: [react(), versionFile()],
  define: { __BUILD_ID__: JSON.stringify(BUILD_ID) },
  // No inlined data: URLs (the CSP allows fonts and scripts from this origin only).
  build: { sourcemap: false, target: "es2022", assetsInlineLimit: 0 },
  server: { port: 5173 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
