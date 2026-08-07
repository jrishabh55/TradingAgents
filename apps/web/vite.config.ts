import { defineConfig } from 'vite'
import { devtools } from '@tanstack/devtools-vite'

import { tanstackStart } from '@tanstack/react-start/plugin/vite'

import viteReact from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { cloudflare } from '@cloudflare/vite-plugin'

/* WEBAPP1_API_BASE: in dev we proxy /api/* to the FastAPI backend so the
   frontend can use a same-origin path and EventSource works without CORS. In
   prod the Cloudflare Worker (configured via wrangler.jsonc env vars) does the
   same proxying. Override with WEBAPP1_API_BASE=http://your-backend:8080. */
const API_BACKEND =
  process.env.WEBAPP1_API_BASE ?? 'http://127.0.0.1:8080'

/* VITE_ALLOWED_HOSTS: comma-separated public hostnames this server may answer
   for. Vite rejects requests whose Host header it doesn't recognise ("Blocked
   request. This host is not allowed."), which is a DNS-rebinding guard — good
   default, but it means a reverse proxy forwarding a real domain (Dokploy →
   Traefik → this container) gets blocked until the domain is listed here.
   Unset leaves Vite's own defaults untouched, so local dev is unaffected. */
const ALLOWED_HOSTS = process.env.VITE_ALLOWED_HOSTS?.split(',')
  .map((h) => h.trim())
  .filter(Boolean)

const config = defineConfig({
  resolve: { tsconfigPaths: true },
  server: {
    port: 3000,
    ...(ALLOWED_HOSTS?.length ? { allowedHosts: ALLOWED_HOSTS } : {}),
    proxy: {
      '/api': {
        target: API_BACKEND,
        changeOrigin: true,
        ws: false,
        configure: (proxy) => {
          /* SSE: do not buffer responses, do not rewrite headers. */
          proxy.on('proxyRes', (res) => {
            if (res.headers['content-type']?.includes('text/event-stream')) {
              res.headers['cache-control'] = 'no-cache'
            }
          })
        },
      },
    },
  },
  plugins: [
    devtools(),
    cloudflare({ viteEnvironment: { name: 'ssr' } }),
    tailwindcss(),
    tanstackStart(),
    viteReact(),
  ],
})

export default config
