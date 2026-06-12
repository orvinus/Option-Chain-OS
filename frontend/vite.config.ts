import path from "node:path";
import { fileURLToPath } from "node:url";

import type { ProxyOptions } from "vite";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.join(frontendDir, "..");

/**
 * HTTP origin for Vite's /api and /ws proxy (must be http(s), never ws(s)).
 * Uses 127.0.0.1 instead of "localhost" so Node does not prefer IPv6 ::1 when
 * uvicorn is only listening on IPv4 (common WebSocket proxy failure on Windows).
 */
function proxyHttpOrigin(env: Record<string, string>): string {
  let raw = (env.VITE_API_BASE || "http://127.0.0.1:8000").trim().replace(/\/$/, "");
  if (!/^https?:\/\//i.test(raw)) {
    raw = "http://127.0.0.1:8000";
  }
  try {
    const u = new URL(raw);
    if (u.hostname === "localhost") {
      u.hostname = "127.0.0.1";
    }
    return u.origin;
  } catch {
    return "http://127.0.0.1:8000";
  }
}

function wsProxyWithLogs(target: string): ProxyOptions {
  return {
    target,
    ws: true,
    changeOrigin: true,
    configure(proxy) {
      proxy.on("error", (err) => {
        console.error("[vite /ws proxy]", err.message);
      });
      proxy.on("proxyReqWs", (_proxyReq, _req, socket) => {
        socket.on("error", (err: NodeJS.ErrnoException) => {
          console.error("[vite /ws proxy] socket:", err.code ?? err.message);
        });
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, repoRoot, "");
  const apiTarget = proxyHttpOrigin(env);

  return {
    envDir: repoRoot,
    plugins: [react()],
    server: {
      port: 5173,
      host: true,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
        },
        "/ws": wsProxyWithLogs(apiTarget),
      },
    },
    build: {
      outDir: "dist",
      sourcemap: true,
    },
  };
});
