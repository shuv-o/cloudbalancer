import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    // In development the panel runs on its own port, so API calls are proxied
    // to Django. In production Nginx serves both from one origin and none of
    // this applies.
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: false },
      "/admin": { target: "http://localhost:8000", changeOrigin: false },
      "/static": { target: "http://localhost:8000", changeOrigin: false },
    },
  },
});
