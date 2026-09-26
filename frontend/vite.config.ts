import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In docker-compose the backend is reachable as http://backend:8000; locally it's localhost.
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      // Backend routes live under /api/v1, so paths pass through unchanged.
      "/api": {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/setupTests.ts"],
  },
});
