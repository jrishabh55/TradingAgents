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

const config = defineConfig({
  resolve: { tsconfigPaths: true },
  server: {
    port: 3000,
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
