import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The production bundle is written into the Python package so `opentune-lab serve`
// can serve it without Node.js. `npm run dev` proxies the API to a running server.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/opentune/lab/web/dist", emptyOutDir: true, chunkSizeWarningLimit: 900 },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/vendor": "http://127.0.0.1:8765",
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
});
