import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/manager": { target: "http://localhost:8001", changeOrigin: true },
      "/v1": { target: "http://localhost:8001", changeOrigin: true }
    }
  }
});
