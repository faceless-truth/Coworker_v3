import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

// Backend dev server runs on 8001 (see infra/systemd/coworker-api.service
// and the README's `uv run uvicorn ... --port 8001`). The Vite dev server
// proxies API calls there so the browser sees same-origin and the session
// cookie flows naturally.
const API_TARGET = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8001";

const API_PATHS = [
  "/health",
  "/auth",
  "/approval",
  "/mail",
  "/webhooks",
] as const;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: Object.fromEntries(
      API_PATHS.map((p) => [
        p,
        {
          target: API_TARGET,
          changeOrigin: true,
          // Preserve cookies on cross-origin proxy so the session JWT
          // survives /auth -> /approval/* navigation.
          cookieDomainRewrite: "",
        },
      ]),
    ),
  },
});
