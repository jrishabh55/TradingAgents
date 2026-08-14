import { clerkMiddleware } from '@clerk/tanstack-react-start/server'
import { createStart } from '@tanstack/react-start'

/* TanStack Start auto-discovers `src/start.ts` (same convention as
   `src/router.tsx`) and runs its requestMiddleware on every request that
   passes through the Start server — page loads AND server routes like
   `src/routes/api.$.tsx`. clerkMiddleware() parses the Clerk session cookie
   and populates the request context that `auth()`/`ClerkProvider` read
   downstream (see apps/web/src/routes/api.$.tsx and __root.tsx).

   By default clerkMiddleware() does NOT protect anything — every route stays
   public; it only makes the parsed session available. Reads
   CLERK_PUBLISHABLE_KEY (falls back to VITE_CLERK_PUBLISHABLE_KEY, which is
   already set) and CLERK_SECRET_KEY from process.env — see apps/web/.env and
   docker-compose.dev.yml's web service. */
export const startInstance = createStart(() => {
  return {
    requestMiddleware: [clerkMiddleware()],
  }
})
