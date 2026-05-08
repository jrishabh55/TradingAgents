import {
  ClerkProvider,
  Show,
  SignInButton,
  SignUpButton,
  UserButton,
} from '@clerk/react'
import { HeadContent, Scripts, createRootRoute } from '@tanstack/react-router'
import { TanStackRouterDevtoolsPanel } from '@tanstack/react-router-devtools'
import { TanStackDevtools } from '@tanstack/react-devtools'

import appCss from '../styles.css?url'

/* Vite exposes any env var prefixed with VITE_* to the browser bundle.
   The publishable key is safe to ship — it identifies the Clerk app, not a
   credential. The secret key (if Clerk hands you one) stays out of this
   project entirely; the FastAPI backend verifies JWTs via public JWKs. */
const PUBLISHABLE_KEY = (
  import.meta as unknown as { env?: { VITE_CLERK_PUBLISHABLE_KEY?: string } }
).env?.VITE_CLERK_PUBLISHABLE_KEY

if (!PUBLISHABLE_KEY) {
  /* Fail loud at boot rather than silently going unauthed. The app is
     architecturally Clerk-first; running without it produces confusing
     401s the moment a request hits the backend with CLERK_JWKS_URL set. */
  // eslint-disable-next-line no-console
  console.error(
    'VITE_CLERK_PUBLISHABLE_KEY is not set. Add it to apps/web/.env. ' +
      'See apps/api/CLERK_SETUP.md for the full setup walkthrough.',
  )
}

export const Route = createRootRoute({
  head: () => ({
    meta: [
      { charSet: 'utf-8' },
      { name: 'viewport', content: 'width=device-width, initial-scale=1' },
      { title: 'Drishti — TradingAgents' },
      { name: 'color-scheme', content: 'dark' },
    ],
    links: [{ rel: 'stylesheet', href: appCss }],
  }),
  shellComponent: RootDocument,
})

function RootDocument({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        <HeadContent />
      </head>
      <body className="font-sans antialiased">
        <ClerkProvider publishableKey={PUBLISHABLE_KEY ?? ''}>
          {/* Show controls visibility based on auth state — Clerk Core 3
              replaced <SignedIn>/<SignedOut> with <Show when="..."> in v6. */}
          <Show when="signed-out">
            {/* Block the entire app behind sign-in. The FastAPI backend
                rejects unauthenticated requests when CLERK_JWKS_URL is set,
                so rendering authenticated UI before sign-in would just
                produce 401-ridden error states. */}
            <SignInGate />
          </Show>
          <Show when="signed-in">
            <UserBadge />
            {children}
          </Show>
        </ClerkProvider>
        <TanStackDevtools
          config={{ position: 'bottom-right' }}
          plugins={[
            { name: 'TanStack Router', render: <TanStackRouterDevtoolsPanel /> },
          ]}
        />
        <Scripts />
      </body>
    </html>
  )
}

function SignInGate() {
  /* Sign-in/up buttons open Clerk's hosted modal by default — no extra
     route plumbing or path config needed, which is friendly for an SPA. */
  return (
    <div
      style={{
        display: 'grid',
        placeItems: 'center',
        minHeight: '100vh',
        background: 'var(--bg, #0a0a0a)',
        gap: 16,
      }}
    >
      <h1 style={{ color: 'var(--text-1, #fff)', fontSize: 28, fontWeight: 600 }}>
        Drishti
      </h1>
      <div style={{ display: 'flex', gap: 12 }}>
        <SignInButton mode="modal">
          <button
            type="button"
            style={{
              padding: '8px 16px',
              borderRadius: 6,
              background: 'var(--accent, #4f46e5)',
              color: '#fff',
              border: 'none',
              cursor: 'pointer',
            }}
          >
            Sign in
          </button>
        </SignInButton>
        <SignUpButton mode="modal">
          <button
            type="button"
            style={{
              padding: '8px 16px',
              borderRadius: 6,
              background: 'transparent',
              color: 'var(--text-1, #fff)',
              border: '1px solid var(--border, #333)',
              cursor: 'pointer',
            }}
          >
            Sign up
          </button>
        </SignUpButton>
      </div>
    </div>
  )
}

function UserBadge() {
  /* Tiny floating badge so the sign-out / account menu is reachable
     without restructuring the existing layout. Positioned to not collide
     with the TanStack devtools panel. */
  return (
    <div
      style={{
        position: 'fixed',
        top: 12,
        right: 12,
        zIndex: 50,
      }}
    >
      <UserButton />
    </div>
  )
}
