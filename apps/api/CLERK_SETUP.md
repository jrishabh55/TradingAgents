# Wiring Clerk auth

The backend is **code-complete for Clerk**: the auth middleware in
`apps/api/auth.py` will verify Clerk JWTs and populate `request.state.user_id`
on every API request. Two env vars are all you need to flip it on.

## 1. Create a Clerk application

1. Sign up at https://clerk.com (free up to 10k MAU on the Pro tier).
2. Create a new application — pick the providers you want (email, Google, GitHub, etc.).
3. From **API Keys** in the dashboard, you'll need:
   - **Publishable Key** (`pk_test_...` or `pk_live_...`) — used by the frontend
   - **Frontend API URL** (e.g. `https://<your-app>.clerk.accounts.dev`) — used to derive the JWKs URL

The **Secret Key** is NOT needed by this backend (we verify JWTs with the public JWKs, not with a session API).

## 2. Set backend env vars

In `.env` (or your deployment's env config):

```
CLERK_JWKS_URL=https://<your-app>.clerk.accounts.dev/.well-known/jwks.json
CLERK_ISSUER=https://<your-app>.clerk.accounts.dev
```

`CLERK_ISSUER` is optional but **strongly recommended** — without it, a JWT
from any Clerk app on the same JWKs cluster could authenticate.

Boot the API. You should see in the logs:
```
Clerk auth enabled (CLERK_JWKS_URL configured)
```

If you instead see *"Auth disabled — every request runs as 'anonymous'"*,
the env var didn't reach the process. Common causes: wrong `.env` path,
forgot to restart the container, or `WEBAPP_CORS_ORIGINS`/etc. set but
`CLERK_JWKS_URL` typo'd.

## 3. Wire up the frontend

In `apps/web/`:

```sh
pnpm add @clerk/clerk-react
```

Add to `apps/web/.env`:
```
VITE_CLERK_PUBLISHABLE_KEY=pk_test_...
```

Wrap your route tree at `apps/web/src/routes/__root.tsx`:

```tsx
import { ClerkProvider, SignedIn, SignedOut, SignInButton, UserButton, useAuth } from '@clerk/clerk-react'

const PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY

export const Route = createRootRoute({
  component: () => (
    <ClerkProvider publishableKey={PUBLISHABLE_KEY}>
      <SignedOut><SignInButton /></SignedOut>
      <SignedIn>
        <UserButton />
        <Outlet />
      </SignedIn>
    </ClerkProvider>
  ),
})
```

Update the API client (`apps/web/src/lib/api.ts`) to attach the JWT:

```ts
import { useAuth } from '@clerk/clerk-react'

export function useApi() {
  const { getToken } = useAuth()
  return {
    async post(path: string, body: unknown) {
      const token = await getToken()
      return fetch(path, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(body),
      })
    },
    // ... GET, etc.
  }
}
```

For SSE (`apps/web/src/lib/sse.ts`), native EventSource can't send headers,
so attach the JWT as a query parameter:

```ts
const token = await getToken()
const url = new URL(api.eventsUrl(runId), location.origin)
if (token) url.searchParams.set('token', token)
const es = new EventSource(url.toString())
```

The backend reads the `?token=<jwt>` query param when the
`Authorization` header is absent (see `apps/api/auth.py::_extract_bearer`).

## 4. Verify end-to-end

1. Boot the backend with `CLERK_JWKS_URL` set.
2. Boot the frontend with `VITE_CLERK_PUBLISHABLE_KEY` set.
3. Open the app. You should be redirected to Clerk's hosted sign-in.
4. Sign in. The app should load with your `<UserButton />` showing your account.
5. Trigger a run. Check the database:
   ```sh
   sqlite3 ~/.tradingagents/webapp.sqlite "SELECT id, ticker, user_id FROM runs ORDER BY created_at DESC LIMIT 5;"
   ```
   The `user_id` should be your Clerk `user_2...` id, not NULL.

## What changes for users

- **All `/api/*` routes** now require a valid Clerk JWT.
- **`GET /api/runs`** only returns the calling user's runs.
- **`GET /api/runs/{id}`**, `cancel`, `report.md`, and `events` return 404 if
  the run belongs to a different user (404, not 403, to avoid confirming the
  run's existence to a non-owner).
- **`POST /api/runs`** creates runs owned by the calling user. The
  same-ticker active-run check is now per-user — User A's TSLA run no longer
  blocks User B from running TSLA.
- **The cache is shared** across users: the report content is a public-data
  analysis, not user-specific, so cache hits cross user boundaries. To make
  it per-user, change `find_cached_run(req_hash, ttl_seconds=...)` in
  `apps/api/routes/runs.py` to pass `user_id=user_id`.

## Falling back

If Clerk goes down or you need to revert temporarily:

- Unset `CLERK_JWKS_URL` (or set it to the empty string).
- The middleware falls back to the legacy shared-bearer (`WEBAPP_AUTH_TOKEN`)
  if that's set, otherwise to fully open mode.

No code changes needed — the auth strategy is selected at request time based
on env vars.

## Limitations of this implementation

- **No org/team support yet.** Every Clerk user is a separate tenant. If you
  add Clerk Organizations, you'd want to read the `org_id` claim from the JWT
  and add it to the runs table next to `user_id`.
- **No revocation list.** A leaked JWT is valid until its `exp` (typically
  60s for Clerk session tokens). For higher-stakes use cases, add Clerk's
  session-API check on top.
- **No rate limiting per user.** Cache + concurrency limits prevent the worst
  cases, but a determined user could still rack up API spend. Add a quota
  table once billing matters (see TASKS.md task 6).
