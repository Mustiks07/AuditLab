import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built assets go into the Python package so `python -m auditlab.demo serve`
// works offline without Node. No CDN, no external fonts.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../auditlab/demo/static",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
  },
  server: {
    proxy: { "/demo": "http://127.0.0.1:8765" },
  },
});
