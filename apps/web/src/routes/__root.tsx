import { ClerkProvider, Show, SignIn } from '@clerk/react'
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
      { title: 'Drishiti — TradingAgents' },
      { name: 'color-scheme', content: 'dark' },
    ],
    links: [{ rel: 'stylesheet', href: appCss }],
  }),
  shellComponent: RootDocument,
})

function RootDocument({ children }: { children: React.ReactNode }) {
  return (
    /* suppressHydrationWarning on BOTH <html> and <body> — browser
       extensions (Grammarly, LastPass, dark-mode tools) inject
       `data-gr-*` / `data-lastpass-*` attributes on <body> between SSR
       and hydration. Without this, React logs a hydration mismatch
       warning every page load. The warning is purely cosmetic — the
       actual app hydration is fine. */
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        <HeadContent />
      </head>
      <body className="font-sans antialiased" suppressHydrationWarning>
        <ClerkProvider
          publishableKey={PUBLISHABLE_KEY ?? ''}
          /* Without these, Clerk redirects post-sign-in to its hosted
             Account Portal at <app>.accounts.dev instead of back to
             your localhost. fallback variants only redirect when no
             explicit ?redirect_url is in the query, which is what we
             want — links into the app from elsewhere still work. */
          signInFallbackRedirectUrl="/"
          signUpFallbackRedirectUrl="/"
          afterSignOutUrl="/"
        >
          {/* Clerk Core 3 replaced <SignedIn>/<SignedOut> with
              <Show when="..."> in v6. */}
          <Show when="signed-out">
            <SignInGate />
          </Show>
          <Show when="signed-in">
            {/* The UserButton (sign-out / account menu) lives in the
                Topbar now — it owns the layout slot, no fixed-position
                overlay fighting with page buttons. See
                apps/web/src/components/shared/Topbar.tsx. */}
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
  /* Embedded sign-in: full Clerk form rendered inline rather than the
     modal pop-up flow. The form handles email + password + any social
     providers configured in the Clerk dashboard. routing="hash" tracks
     sign-in state via the URL hash (#/factor-one, #/sso-callback, etc.)
     without needing dedicated app routes. */
  return (
    <main
      style={{
        minHeight: '100vh',
        background: 'var(--bg-0)',
        display: 'grid',
        gridTemplateColumns: '1fr',
        placeItems: 'center',
        padding: '24px',
      }}
    >
      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: 24,
          maxWidth: 480,
          width: '100%',
        }}
      >
        <header style={{ textAlign: 'center' }}>
          <h1
            style={{
              fontSize: 36,
              fontWeight: 600,
              color: 'var(--text-1)',
              letterSpacing: '-0.02em',
              margin: 0,
            }}
          >
            Drishiti
          </h1>
          <p
            style={{
              color: 'var(--text-3)',
              marginTop: 8,
              fontSize: 14,
            }}
          >
            Multi-agent trading analysis
          </p>
        </header>
        {/* The Clerk widget styles itself with appearance tokens. We
            tune a small set so it stops feeling like a third-party
            iframe — dark surface, our accent color, and tight padding
            so the embedded form sits cleanly in the dark UI. */}
        <SignIn
          routing="hash"
          /* forceRedirectUrl ensures we always come back to / after a
             successful sign-in, even on multi-step flows that might
             otherwise hand off to the hosted portal. */
          forceRedirectUrl="/"
          appearance={{
            variables: {
              colorPrimary: '#4f7cff',
              colorBackground: '#101218',
              colorText: '#e7e9ef',
              colorInputBackground: '#161922',
              colorInputText: '#e7e9ef',
              colorTextSecondary: '#b4b9c6',
              colorTextOnPrimaryBackground: '#ffffff',
              colorNeutral: '#7d8499',
              borderRadius: '8px',
              fontFamily: 'inherit',
            },
            elements: {
              card: {
                background: 'var(--bg-1)',
                border: '1px solid var(--bg-3)',
                boxShadow: '0 8px 32px rgba(0, 0, 0, 0.32)',
              },
              headerTitle: { color: 'var(--text-1)' },
              headerSubtitle: { color: 'var(--text-3)' },
              socialButtonsBlockButton: {
                background: 'var(--bg-2)',
                border: '1px solid var(--bg-3)',
                color: 'var(--text-1)',
              },
              formFieldInput: {
                background: 'var(--bg-2)',
                border: '1px solid var(--bg-3)',
                color: 'var(--text-1)',
              },
              footerActionLink: { color: 'var(--accent-hi)' },
            },
          }}
        />
      </div>
    </main>
  )
}

