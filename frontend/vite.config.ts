import { resolve } from 'path'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// Build output lands in ../static so FastAPI (app/main.py) can serve the SPA
// directly — see docs/ARCHITECTURE.md "Platform service". `base: '/static/'`
// matches the mount path main.py uses for StaticFiles.
const API = process.env.VITE_API_TARGET ?? 'http://localhost:8000'

export default defineConfig(({ command }) => ({
  plugins: [vue()],
  base: command === 'build' ? '/static/' : '/',
  build: {
    outDir: '../static',
    emptyOutDir: true,
    // Never inline fonts as data: URIs; the CSP is font-src 'self' (small unicode-range subsets would otherwise inline).
    assetsInlineLimit: (file) => (file.endsWith('.woff2') ? false : undefined),
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, './src'),
      '@docs': resolve(__dirname, '../docs'),
    },
  },
  server: {
    fs: { allow: ['..'] },
    // The API sits at the root and shares paths with SPA routes (/apps, /admin), so page loads stay here and everything
    // else that isn't Vite's own goes to the API.
    proxy: {
      '^/(?!@|src/|node_modules/|favicon|theme-init|site\\.webmanifest|.*\\.(png|svg|ico|woff2)$)': {
        target: API,
        bypass: (req) => (req.headers.accept?.includes('text/html') ? '/index.html' : undefined),
      },
    },
  },
}))
