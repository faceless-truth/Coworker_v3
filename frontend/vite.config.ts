import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'https://coworker.mcands.com.au',
        changeOrigin: true,
        secure: true,
        cookieDomainRewrite: 'localhost',
      },
      '/health': {
        target: 'https://coworker.mcands.com.au',
        changeOrigin: true,
        secure: true,
      },
    },
  },
})
