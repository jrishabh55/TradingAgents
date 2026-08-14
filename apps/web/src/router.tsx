import { createRouter as createTanStackRouter } from '@tanstack/react-router'
import { ApiError } from './lib/api'
import { routeTree } from './routeTree.gen'

/* Route loaders fire on navigation regardless of auth state, so a signed-out
   hard load of a data route gets a legitimate 401 before the root <Show>
   gates can render the sign-in screen — and an uncaught loader error would
   replace the whole tree (sign-in gate included) with an error page. A 401
   is not an error here; it's "the auth gates own this screen": render
   nothing and let __root's signed-out branch take over. */
function DefaultRouteError({ error }: { error: Error }) {
  if (error instanceof ApiError && error.status === 401) return null
  return (
    <main className="grid min-h-[50vh] place-items-center p-6">
      <div className="max-w-lg space-y-2 text-center">
        <h1 className="text-lg font-semibold">Something went wrong</h1>
        <p className="font-mono text-sm text-red-500">{error.message}</p>
      </div>
    </main>
  )
}

export function getRouter() {
  const router = createTanStackRouter({
    routeTree,
    scrollRestoration: true,
    defaultPreload: 'intent',
    defaultPreloadStaleTime: 0,
    defaultErrorComponent: DefaultRouteError,
    /* A 401'd loader (signed-out hard load) leaves the route's cache entry in
       an error state; after sign-in the same navigation must refetch rather
       than replay the cached failure. */
    defaultGcTime: 0,
  })

  return router
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof getRouter>
  }
}
