import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // ws: true so streamed pre-fill sessions (/api/prefill-sessions/*/stream)
      // proxy through the dev server as WebSockets.
      "/api": { target: "http://127.0.0.1:8000", ws: true },
    },
  },
});
