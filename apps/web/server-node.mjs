/* Production Node server for the built TanStack Start app.
 *
 * `WEB_TARGET=node vite build` (see vite.config.ts) emits:
 *   dist/client/  — hashed static assets
 *   dist/server/  — the app as a WinterCG fetch handler (SSR + the /api/$
 *                   proxy route, which reads WEBAPP1_API_BASE from process.env
 *                   at request time)
 *
 * This file is the thin listener around that handler: static files straight
 * from dist/client, everything else into the app. srvx does the fetch↔node
 * plumbing, including streaming Response bodies — which is what keeps SSE
 * (/api/runs/{id}/events) working through the proxy.
 *
 * Why not `vite dev` in prod (the old setup): the dev server keeps a live HMR
 * websocket in every open tab, and a redeploy hot-patches running pages into
 * a corrupted module graph. Hashed immutable assets make deploys atomic.
 */
import { readFile, stat } from 'node:fs/promises'
import { extname, join, normalize, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serve } from 'srvx'
import app from './dist/server/server.js'

const CLIENT_DIR = fileURLToPath(new URL('./dist/client', import.meta.url))

const MIME = {
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.webmanifest': 'application/manifest+json',
}

async function serveStatic(pathname) {
  // Resolve inside dist/client only — reject traversal.
  const file = normalize(join(CLIENT_DIR, pathname))
  if (!file.startsWith(CLIENT_DIR + sep)) return null
  try {
    const s = await stat(file)
    if (!s.isFile()) return null
    return new Response(await readFile(file), {
      headers: {
        'content-type': MIME[extname(file)] ?? 'application/octet-stream',
        // Vite content-hashes everything under /assets — cache forever.
        // The rest (favicon, manifest) may change between deploys.
        'cache-control': pathname.startsWith('/assets/')
          ? 'public, max-age=31536000, immutable'
          : 'public, max-age=3600',
      },
    })
  } catch {
    return null
  }
}

const port = Number(process.env.PORT ?? 3000)

serve({
  port,
  hostname: '0.0.0.0',
  fetch: async (req) => {
    if (req.method === 'GET' || req.method === 'HEAD') {
      const { pathname } = new URL(req.url)
      // Only paths that look like files — /runs/123 must fall through to SSR.
      if (pathname !== '/' && extname(pathname) !== '') {
        const hit = await serveStatic(decodeURIComponent(pathname))
        if (hit) return hit
      }
    }
    return app.fetch(req)
  },
})

console.log(`drishti web: serving dist/ on :${port}`)
