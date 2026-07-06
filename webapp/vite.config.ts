import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// TMA отдаётся с корня goldcut.salmetov.fun; /api проксируется на бэкенд в dev.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": "http://127.0.0.1:18090" },
  },
});
