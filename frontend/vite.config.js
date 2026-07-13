import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/strategies': 'http://localhost:8000',
      '/portfolio': 'http://localhost:8000',
      '/activity': 'http://localhost:8000',
      '/data': 'http://localhost:8000',
      '/trades': 'http://localhost:8000',
      '/risk': 'http://localhost:8000',
      '/live': 'http://localhost:8000',
      '/token': 'http://localhost:8000',
      '/dhan': 'http://localhost:8000'
    }
  },
  build: {
    outDir: '../app/static',
    emptyOutDir: true,
    minify: 'esbuild'  // Vite default; terser is an optional dep we don't install
  }
})
