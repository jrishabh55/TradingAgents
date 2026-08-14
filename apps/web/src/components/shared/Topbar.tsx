import { UserButton } from '@clerk/tanstack-react-start'
import { Link } from '@tanstack/react-router'
import { useEffect, useState } from 'react'

import { api } from '../../lib/api'

/** Remaining run credits (Clerk privateMetadata, served by /api/me).
 *
 *  Fetches on mount — the Topbar remounts on navigation, and the backend
 *  caches the gate lookup, so this stays cheap and current (a run debit
 *  updates the server cache immediately). Renders nothing while loading or
 *  when the deployment has no credit gate (`credits: null`). */
function CreditsPill() {
  const [credits, setCredits] = useState<number | null>(null)

  useEffect(() => {
    let stale = false
    api
      .me()
      .then((me) => !stale && setCredits(me.credits))
      .catch(() => {
        /* gate off, backend down, or not activated — no pill either way */
      })
    return () => {
      stale = true
    }
  }, [])

  if (credits == null) return null
  return (
    <span className={`es-pill ${credits > 0 ? 'ok' : 'err'}`}>
      {credits} credit{credits === 1 ? '' : 's'}
    </span>
  )
}

/** "Get the helper app" link — the download otherwise only surfaces inside
 *  the ChatGPT-subscription provider's setup card, which you'd never see
 *  without selecting that provider first. Shown when no helper is connected
 *  (install nudge) or the connected one is outdated (update nudge); hidden
 *  when up to date or no build is hosted. */
function HelperDownloadLink() {
  const [link, setLink] = useState<{ url: string; update: boolean } | null>(null)

  useEffect(() => {
    let stale = false
    api
      .getHelperStatus()
      .then((s) => {
        if (stale || !s.download_url) return
        if (!s.connected) setLink({ url: s.download_url, update: false })
        else if (s.update_available) setLink({ url: s.download_url, update: true })
      })
      .catch(() => {
        /* backend down or unauthenticated — no link either way */
      })
    return () => {
      stale = true
    }
  }, [])

  if (!link) return null
  return (
    <a
      className="es-btn ghost sm no-underline"
      href={link.url}
      target="_blank"
      rel="noreferrer"
      title={
        link.update
          ? 'A newer helper version is available — update to get the latest features'
          : 'Run analyses on your ChatGPT subscription via the local helper app'
      }
    >
      {link.update ? 'Update the helper ↗' : 'Get the helper app ↗'}
    </a>
  )
}

export interface TopbarProps {
  ticker?: string
  date?: string
  /* `running` shows a yellow live pill, `done` shows a green completed pill,
     `interrupted` an orange warning pill, `idle` (default) shows nothing in
     the right slot. */
  state?: 'idle' | 'running' | 'done' | 'failed' | 'interrupted'
  elapsed?: string | null
  total?: string | null
  onCancel?: () => void
  onExport?: () => void
}

export function Topbar({
  ticker,
  date,
  state = 'idle',
  elapsed,
  total,
  onCancel,
  onExport,
}: TopbarProps) {
  return (
    <div className="es-topbar">
      <Link to="/" className="es-logo no-underline">
        <div className="es-logo-mark">D</div>
        Drishti
      </Link>
      {ticker && (
        <div className="es-crumbs">
          <Link to="/" className="text-[var(--text-3)] no-underline">
            Analyses
          </Link>
          <span className="sep">/</span>
          <span className="now">
            {ticker}
            {date ? ` · ${date}` : ''}
          </span>
        </div>
      )}
      <div className="es-spacer" />
      {state === 'running' && (
        <span className="es-pill run">
          <span className="es-dot pulse" />
          Run in progress
          {elapsed ? ` · ${elapsed}` : ''}
        </span>
      )}
      {state === 'done' && (
        <span className="es-pill ok">
          <span className="es-dot" />
          Run complete
          {total ? ` · ${total}` : ''}
        </span>
      )}
      {state === 'failed' && (
        <span className="es-pill err">
          <span className="es-dot" />
          Run failed
        </span>
      )}
      {state === 'interrupted' && (
        <span className="es-pill warn">
          <span className="es-dot" />
          Run interrupted
        </span>
      )}
      {state === 'running' && onCancel && (
        <button className="es-btn ghost sm" onClick={onCancel}>
          Cancel
        </button>
      )}
      {(state === 'done' || state === 'failed') && onExport && (
        <button className="es-btn sm" onClick={onExport}>
          Export
        </button>
      )}
      <HelperDownloadLink />
      <Link to="/" className="es-btn primary sm no-underline">
        New analysis
      </Link>
      <CreditsPill />
      {/* Sign-out / account menu lives at the end of the topbar so it
          owns its own layout slot — no fixed-position overlay fighting
          with page buttons. */}
      <UserButton />
    </div>
  )
}
