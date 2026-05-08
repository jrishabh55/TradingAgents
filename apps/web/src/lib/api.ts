import type {
  ConfigResponse,
  RunDetail,
  RunRequest,
  RunSummary,
} from './types'

/* Default to /api so the Cloudflare Worker (in prod) or Vite proxy (in dev)
   can route to the FastAPI backend. Override at build time with VITE_API_BASE
   when the backend lives at a different origin. */
const API_BASE: string =
  (import.meta as unknown as { env?: { VITE_API_BASE?: string } }).env
    ?.VITE_API_BASE ?? '/api'

/* Legacy shared bearer — only used when the backend is in WEBAPP_AUTH_TOKEN
   mode. Clerk JWTs (when configured) take precedence and override this. */
const LEGACY_TOKEN: string | undefined = (
  import.meta as unknown as { env?: { VITE_API_TOKEN?: string } }
).env?.VITE_API_TOKEN

/* Clerk attaches itself to window.Clerk once <ClerkProvider> mounts. Reading
   through this typed-any keeps the type surface small and avoids pulling
   @clerk/types into modules that just need a token at call time. */
type ClerkSession = { getToken: () => Promise<string | null> }
type WindowWithClerk = Window & {
  Clerk?: { load?: () => Promise<void>; session?: ClerkSession | null }
}

/** Resolve a JWT for the current session, awaiting Clerk hydration if needed.
 *
 *  Returns:
 *    - the Clerk session JWT when a user is signed in
 *    - the legacy `VITE_API_TOKEN` when no Clerk is configured
 *    - null when neither is available (request goes out unauthed; the
 *      backend may be in open mode, or the request will get HTTP 401)
 *
 *  Safe to call from anywhere — React component, router loader, plain async.
 */
export async function getAuthToken(): Promise<string | null> {
  if (typeof window === 'undefined') return LEGACY_TOKEN ?? null
  const clerk = (window as WindowWithClerk).Clerk
  if (clerk) {
    /* If <ClerkProvider> mounted but the session hasn't hydrated yet, load()
       resolves once it has. Cheap on subsequent calls. */
    if (clerk.load) await clerk.load()
    const token = (await clerk.session?.getToken()) ?? null
    if (token) return token
  }
  return LEGACY_TOKEN ?? null
}

async function authHeaders(): Promise<Record<string, string>> {
  const token = await getAuthToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(await authHeaders()),
      ...(init?.headers ?? {}),
    },
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}: ${text}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  config: () => jsonFetch<ConfigResponse>('/config'),
  listRuns: () => jsonFetch<RunSummary[]>('/runs'),
  getRun: (id: string) => jsonFetch<RunDetail>(`/runs/${id}`),
  createRun: (body: RunRequest) =>
    jsonFetch<RunDetail>('/runs', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  cancelRun: (id: string) =>
    jsonFetch<{ ok: boolean }>(`/runs/${id}/cancel`, { method: 'POST' }),
  reportUrl: (id: string) => `${API_BASE}/runs/${id}/report.md`,
  /* SSE: native EventSource can't send headers, so the JWT is appended as
     a query param. The backend reads ?token= when Authorization is absent
     (apps/api/auth.py::_extract_bearer). The token is fresh per call. */
  eventsUrl: async (id: string): Promise<string> => {
    const base = `${API_BASE}/runs/${id}/events`
    const token = await getAuthToken()
    return token ? `${base}?token=${encodeURIComponent(token)}` : base
  },
}

export { API_BASE }
