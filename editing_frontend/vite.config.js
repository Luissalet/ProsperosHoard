import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  root: '.',
  base: '/editing/',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../prosperos_hoard/editing/static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 700,
  },
  server: {
    host: '127.0.0.1',
    port: Number(process.env.VITE_PORT || 5173),
    proxy: {
      '/api': `http://127.0.0.1:${process.env.PROSPERO_PORT || 8815}`,
      '/editing/api': `http://127.0.0.1:${process.env.PROSPERO_PORT || 8815}`,
      '/studio-assets': `http://127.0.0.1:${process.env.PROSPERO_PORT || 8815}`,
    },
  },
});
