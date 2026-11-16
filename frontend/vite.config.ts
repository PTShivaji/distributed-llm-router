import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// FastAPI backend used by the development proxy. The browser only ever talks to the
// Vite origin, so the backend needs no CORS configuration for local development.
// LLMR_DEV_BACKEND (Node-side only, never bundled) can point the proxy at another local
// instance, e.g. an isolated test backend used for visual checks.
const BACKEND = process.env.LLMR_DEV_BACKEND ?? 'http://127.0.0.1:8000'
const proxied = { target: BACKEND, changeOrigin: false, secure: false }

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': proxied,
      '/health': proxied,
      '/metrics': proxied,
    },
  },
  preview: { host: '127.0.0.1', port: 4173, strictPort: true },
  build: { sourcemap: false },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
    css: false,
    // Per-test budget (default 5 s): page tests with several user interactions need headroom under CPU load.
    testTimeout: 15_000,
  },
})
