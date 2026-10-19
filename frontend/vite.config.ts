import { resolve } from 'path'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// Build output lands in ../static so FastAPI (app/main.py) can serve the SPA
// directly — see docs/ARCHITECTURE.md "Platform service". `base: '/static/'`
// matches the mount path main.py uses for StaticFiles.
export default defineConfig({
  plugins: [vue()],
  base: '/static/',
  build: {
    outDir: '../static',
    emptyOutDir: true,
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, './src'),
      '@docs': resolve(__dirname, '../docs'),
    },
  },
  server: {
    fs: { allow: ['..'] },
    proxy: {
      '/auth': 'http://localhost:8000',
      '/apps': 'http://localhost:8000',
      '/api': 'http://localhost:8000',
    },
  },
})
