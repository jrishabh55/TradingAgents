import { createFileRoute } from '@tanstack/react-router'

/* Catch-all proxy for /api/* so the browser stays same-origin. The Cloudflare
   Worker forwards every request (including SSE) to the FastAPI backend at
   WEBAPP1_API_BASE. SSE streams pass through unchanged because we hand the
   upstream Response straight back — Workers will stream the body. */

const ALLOWED_METHODS = [
  'GET',
  'POST',
  'PUT',
  'PATCH',
  'DELETE',
  'OPTIONS',
] as const

async function proxy(req: Request): Promise<Response> {
  /* Read the upstream base from the worker env, falling back to the dev
     default. process.env is populated from wrangler.jsonc `vars` at runtime. */
  const base =
    (typeof process !== 'undefined' &&
      (process.env.WEBAPP1_API_BASE as string | undefined)) ||
    'http://127.0.0.1:8080'

  const url = new URL(req.url)
  const upstream = new URL(
    `/api${url.pathname.replace(/^\/api/, '')}${url.search}`,
    base,
  )

  const init: RequestInit = {
    method: req.method,
    headers: req.headers,
    redirect: 'manual',
  }
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    init.body = req.body
    /* duplex is required when streaming a request body in modern fetch. */
    ;(init as RequestInit & { duplex?: string }).duplex = 'half'
  }

  const upstreamRes = await fetch(upstream, init)
  /* Pass through the response — for SSE the body is a ReadableStream and the
     Worker streams it. */
  return new Response(upstreamRes.body, {
    status: upstreamRes.status,
    statusText: upstreamRes.statusText,
    headers: upstreamRes.headers,
  })
}

const handlers = Object.fromEntries(
  ALLOWED_METHODS.map((m) => [m, ({ request }: { request: Request }) => proxy(request)]),
)

export const Route = createFileRoute('/api/$')({
  server: {
    handlers,
  },
})
