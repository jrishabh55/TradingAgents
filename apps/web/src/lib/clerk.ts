import { Clerk } from '@clerk/clerk-js'

/* The single Clerk instance for the whole app — constructed here, before any
   React code runs, and handed to <ClerkProvider Clerk={clerk}> in __root.tsx.

   Why: route loaders run BEFORE the React tree mounts on a hard page load.
   The default ClerkProvider setup creates the instance as a side effect of
   rendering, so anything outside React (loaders, api.ts) had no reliable
   handle on it — reading window.Clerk was a race. Owning the instance in a
   plain module makes "wait for auth" an ordinary promise instead of a poll.
   This mirrors Clerk's own pre-constructed-instance pattern (their Expo SDK
   passes an instance to the provider the same way). */
const PUBLISHABLE_KEY = (
  import.meta as unknown as { env?: { VITE_CLERK_PUBLISHABLE_KEY?: string } }
).env?.VITE_CLERK_PUBLISHABLE_KEY

export const clerk = new Clerk(PUBLISHABLE_KEY ?? '')

/** Resolves once clerk-js has loaded (ClerkProvider triggers the load when the
 *  root shell mounts — always before any loader's fetch needs the result).
 *  SSR-safe: resolves immediately off the browser. */
export function clerkReady(): Promise<void> {
  if (typeof window === 'undefined' || clerk.loaded) return Promise.resolve()
  return new Promise((resolve) => {
    /* `status` moves loading → ready|degraded; both mean load() finished
       (degraded = loaded but some Clerk services impaired — auth still works).
       notify:true fires the handler immediately with the current status, which
       closes the gap where load finished between our `loaded` check and the
       subscription. */
    const handler = (status: string) => {
      if (status === 'ready' || status === 'degraded') {
        clerk.off('status', handler)
        resolve()
      }
    }
    clerk.on('status', handler, { notify: true })
  })
}
