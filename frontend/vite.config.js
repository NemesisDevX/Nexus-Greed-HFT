import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// streamer lives on :8000, hit it direct — CORS is open
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    open: true,
  },
})
