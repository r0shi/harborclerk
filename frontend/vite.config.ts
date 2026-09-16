import { fileURLToPath } from 'node:url'
// `vitest/config`, not `vite`. Vitest 5 moved the `UserConfig.test` module
// augmentation behind this entry point, so the old `/// <reference
// types="vitest" />` is inert and `test:` below stops type-checking. Silent
// here, because tsconfig.json only includes `src` — the editor shows it, CI
// never does.
import { defineConfig } from 'vitest/config'
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
