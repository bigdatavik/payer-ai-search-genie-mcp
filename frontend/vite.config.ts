import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build the SPA into ../static so FastAPI can serve it as one process/port.
// `base: "./"` keeps asset URLs relative, so it works behind the Databricks
// Apps subdomain regardless of path prefix.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    // Local dev: `npm run dev` serves the UI on :5173 and proxies API calls to
    // the FastAPI backend on :8000 (run `python app.py` in another terminal).
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
