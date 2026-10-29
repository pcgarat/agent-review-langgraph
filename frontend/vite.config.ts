import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// `base: '/'` y no `'./'`: el panel se sirve en la raiz del proceso `web`. Con
// rutas como `/review/<review_key>` un base relativo romperia los assets en las
// rutas profundas publicadas en las tarjetas de Teams (spec 029, P4).
export default defineConfig({
  base: '/',
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, 'src') },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
  server: {
    port: 5173,
    // La cookie de sesion es del mismo origen tambien en desarrollo: Vite
    // reenvia `/api` al proceso `web` y el navegador no ve dos origenes.
    // `PANEL_WEB_URL` permite apuntar a otro puerto sin editar el fichero.
    proxy: {
      '/api': { target: process.env.PANEL_WEB_URL ?? 'http://localhost:8090', changeOrigin: false },
      '/healthz': {
        target: process.env.PANEL_WEB_URL ?? 'http://localhost:8090',
        changeOrigin: false,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
