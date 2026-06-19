import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the API to the backend.
// - Proxy target is configurable via VITE_DEV_PROXY_TARGET (default localhost:8000)
//   so container / remote-dev setups can point elsewhere without editing this file.
// - The server binds to localhost by default; set VITE_DEV_HOST=true to expose it
//   on all interfaces (e.g. inside a container) — opt-in to avoid leaking the
//   /api proxy onto the network unintentionally.
// - SSE endpoints under /api/events must NOT be buffered; the proxy passes them
//   through unmodified. WebSocket upgrade is enabled for /api/ws via `ws: true`.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxyTarget = env.VITE_DEV_PROXY_TARGET || "http://localhost:8000";
  const exposeHost = env.VITE_DEV_HOST === "true";

  return {
    plugins: [react()],
    server: {
      host: exposeHost ? true : "localhost",
      port: 5173,
      proxy: {
        "/api": {
          target: proxyTarget,
          changeOrigin: true,
          ws: true,
        },
      },
    },
    build: {
      outDir: "dist",
      sourcemap: false,
      chunkSizeWarningLimit: 4000,
      rollupOptions: {
        output: {
          // Split heavy visualization libraries into cacheable lazy chunks. Use a function so deep
          // Plotly modular imports (plotly.js/lib/*) stay grouped in the plotly chunk.
          manualChunks(id) {
            if (id.includes("/node_modules/plotly.js/") || id.includes("/node_modules/react-plotly.js/")) {
              return "plotly";
            }
            if (id.includes("/node_modules/ngl/")) {
              return "ngl";
            }
            if (
              id.includes("/node_modules/react/") ||
              id.includes("/node_modules/react-dom/") ||
              id.includes("/node_modules/react-router-dom/")
            ) {
              return "react-vendor";
            }
          },
        },
      },
    },
  };
});
