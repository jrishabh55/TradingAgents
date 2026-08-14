/* Bridge between Clerk's React lifecycle and code that runs outside React.
 *
 * Route loaders execute before <ClerkProvider> mounts on a hard page load, so
 * they can't use hooks — but they need the session token. Clerk's SDK owns its
 * instance and its loading (including hot-loading the UI bundle), so we don't
 * construct or poll anything: <ClerkReadySignal> (rendered inside
 * <ClerkLoaded> in __root.tsx) hands the loaded instance to this module the
 * moment Clerk says it's ready, resolving the promise loaders await.
 */

/* The minimal surface loaders need — avoids depending on Clerk's full types
   in every module that just wants a token. */
export type ClerkTokenSource = {
  session?: { getToken: () => Promise<string | null> } | null
}

let resolveReady: (clerk: ClerkTokenSource) => void
const ready = new Promise<ClerkTokenSource>((resolve) => {
  resolveReady = resolve
})

/** Called exactly once by <ClerkReadySignal> when Clerk finishes loading. */
export function markClerkReady(clerk: ClerkTokenSource): void {
  resolveReady(clerk)
}

/** Resolves with the loaded Clerk instance. Falls back to null after 15s so a
 *  broken Clerk load surfaces as a 401 instead of a page that hangs forever. */
export function clerkReady(): Promise<ClerkTokenSource | null> {
  return Promise.race([
    ready,
    new Promise<null>((resolve) => setTimeout(() => resolve(null), 15_000)),
  ])
}
