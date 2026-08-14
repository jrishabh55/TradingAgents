import type {
  ConfigResponse,
  GeminiKeyStatus,
  HelperStatus,
  LevelsParams,
  LevelsResponse,
  MeResponse,
  PairResponse,
  RunDetail,
  RunRequest,
  RunSummary,
  TickerHit,
} from './types'
import type { ScanGroup, ScanResult, ScannerSummary } from './scanner-types'

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
const CLERK_CONFIGURED = Boolean(
  (import.meta as unknown as { env?: { VITE_CLERK_PUBLISHABLE_KEY?: string } })
    .env?.VITE_CLERK_PUBLISHABLE_KEY,
)

/* window.Clerk only exists after <ClerkProvider> mounts, but route loaders run
   BEFORE the React tree on a hard page load — returning null there sends the
   loader's fetch out unauthenticated (401 "missing bearer token"). When Clerk
   is configured it always shows up, so wait for it. */
async function waitForClerk(): Promise<WindowWithClerk['Clerk']> {
  const w = window as WindowWithClerk
  if (!CLERK_CONFIGURED) return w.Clerk
  for (let waited = 0; !w.Clerk && waited < 5000; waited += 50) {
    await new Promise((r) => setTimeout(r, 50))
  }
  return w.Clerk
}

export async function getAuthToken(): Promise<string | null> {
  if (typeof window === 'undefined') return LEGACY_TOKEN ?? null
  const clerk = await waitForClerk()
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

/** An HTTP error that keeps the parsed body, so callers can act on it.
 *
 *  Needed because some 4xx bodies are structured — a rejected ticker carries
 *  `detail.suggestions`, which the UI offers as one-tap fixes. Regexing those
 *  back out of a message string would break the moment the wording changes.
 */
export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, statusText: string, detail: unknown, raw: string) {
    const message =
      detail && typeof detail === 'object' && 'message' in detail
        ? String((detail as { message: unknown }).message)
        : typeof detail === 'string'
          ? detail
          : `${status} ${statusText}: ${raw}`
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** Narrow an unknown error to the rejected-ticker shape, if that's what it is. */
export function noMarketDataDetail(
  err: unknown,
): { message: string; ticker: string; suggestions: string[] } | null {
  if (!(err instanceof ApiError)) return null
  const d = err.detail
  if (!d || typeof d !== 'object') return null
  const rec = d as Record<string, unknown>
  if (rec.code !== 'no_market_data') return null
  return {
    message: String(rec.message ?? err.message),
    ticker: String(rec.ticker ?? ''),
    suggestions: Array.isArray(rec.suggestions)
      ? rec.suggestions.map(String)
      : [],
  }
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
    const raw = await res.text().catch(() => '')
    let detail: unknown = raw
    try {
      const parsed = JSON.parse(raw) as { detail?: unknown }
      detail = parsed?.detail ?? parsed
    } catch {
      /* non-JSON body (proxy error page, empty 502) — keep the raw text */
    }
    throw new ApiError(res.status, res.statusText, detail, raw)
  }
  /* 204 No Content (e.g. DELETE /scanners/:id) has no body to parse. */
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

/** True when `err` is the middleware's "account not activated" rejection. */
export function isNotActivated(err: unknown): boolean {
  return (
    err instanceof ApiError &&
    err.status === 403 &&
    typeof err.detail === 'object' &&
    err.detail !== null &&
    (err.detail as Record<string, unknown>).code === 'not_activated'
  )
}

export const api = {
  me: () => jsonFetch<MeResponse>('/me'),
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
  /* Resume an `interrupted` run from its checkpoint. 409 carries a
     human-readable `detail` (no checkpoint, already resuming, limit hit). */
  resumeRun: (id: string) =>
    jsonFetch<RunDetail>(`/runs/${id}/resume`, { method: 'POST' }),
  /* Yahoo Finance ticker typeahead, proxied server-side (Yahoo blocks CORS). */
  searchTickers: (q: string) =>
    jsonFetch<{ results: TickerHit[] }>(
      `/search/tickers?q=${encodeURIComponent(q)}`,
    ),
  getHelperStatus: () => jsonFetch<HelperStatus>('/helper/status'),
  /* Gemini BYOC credential — the key itself never comes back, only last4. */
  getGeminiKeyStatus: () => jsonFetch<GeminiKeyStatus>('/keys/gemini'),
  saveGeminiKey: (apiKey: string) =>
    jsonFetch<GeminiKeyStatus>('/keys/gemini', {
      method: 'PUT',
      body: JSON.stringify({ api_key: apiKey }),
    }),
  deleteGeminiKey: () =>
    jsonFetch<{ deleted: boolean }>('/keys/gemini', { method: 'DELETE' }),
  /* Mint a helper pairing token. The token is shown once and never
     retrievable again; `command` is ready to paste into a shell. */
  pairHelper: () => jsonFetch<PairResponse>('/relay/pair', { method: 'POST' }),
  reportUrl: (id: string) => `${API_BASE}/runs/${id}/report.md`,
  /* Deterministic arithmetic server-side, so it's cheap to re-request as the
     user changes capital or risk — no pipeline re-run. */
  runLevels: (id: string, params: LevelsParams) => {
    const q = new URLSearchParams({ capital: String(params.capital) })
    if (params.risk_pct != null) q.set('risk_pct', String(params.risk_pct))
    if (params.r_multiple != null) q.set('r_multiple', String(params.r_multiple))
    return jsonFetch<LevelsResponse>(`/runs/${id}/levels?${q}`)
  },
  /* SSE: native EventSource can't send headers, so the JWT is appended as
     a query param. The backend reads ?token= when Authorization is absent
     (apps/api/auth.py::_extract_bearer). The token is fresh per call. */
  eventsUrl: async (id: string): Promise<string> => {
    const base = `${API_BASE}/runs/${id}/events`
    const token = await getAuthToken()
    return token ? `${base}?token=${encodeURIComponent(token)}` : base
  },
  listScanners: () => jsonFetch<ScannerSummary[]>('/scanners'),
  createScanner: (body: { name: string; description?: string; definition: ScanGroup }) =>
    jsonFetch<ScannerSummary>('/scanners', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  updateScanner: (
    id: string,
    body: { name: string; description?: string; definition: ScanGroup },
  ) =>
    jsonFetch<ScannerSummary>(`/scanners/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  deleteScanner: (id: string) =>
    jsonFetch<void>(`/scanners/${id}`, { method: 'DELETE' }),
  runScanner: (id: string) =>
    jsonFetch<ScanResult>(`/scanners/${id}/run`, { method: 'POST' }),
  previewScanner: (definition: ScanGroup) =>
    jsonFetch<ScanResult>('/scanners/preview', {
      method: 'POST',
      body: JSON.stringify({ definition }),
    }),
  nlScanner: (prompt: string) =>
    jsonFetch<{ definition: ScanGroup; explanation: string }>('/scanners/nl', {
      method: 'POST',
      body: JSON.stringify({ prompt }),
    }),
}

export { API_BASE }
