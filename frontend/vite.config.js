import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server on 5173; the FastAPI streamer runs on 8000. We connect to it
// directly over WebSocket (CORS is wide-open on the Python side for the demo).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    open: true,
  },
})
