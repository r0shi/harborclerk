/// <reference types="vitest" />
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const frontendRoot = fileURLToPath(new URL('.', import.meta.url))
const skillsRoot = fileURLToPath(new URL('../skills', import.meta.url))

export default defineConfig({
  plugins: [tailwindcss(), react()],
  server: {
    host: 'localhost',
    fs: {
      allow: [frontendRoot, skillsRoot],
    },
    proxy: {
      '/api': 'http://localhost:8000',
      '/mcp': 'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
  },
})
