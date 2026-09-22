import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Placeholder Vite config for the UI agent to build on. The dev server
// proxies /api to the Python backend (run `python -m prosperos_hoard`
// separately on port 8815) so `npm run dev` works against real data.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8815",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
