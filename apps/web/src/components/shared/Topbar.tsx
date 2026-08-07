import { UserButton } from '@clerk/react'
import { Link } from '@tanstack/react-router'

export interface TopbarProps {
  ticker?: string
  date?: string
  /* `running` shows a yellow live pill, `done` shows a green completed pill,
     `idle` (default) shows nothing in the right slot. */
  state?: 'idle' | 'running' | 'done' | 'failed'
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
        Drishiti
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
      <Link to="/" className="es-btn primary sm no-underline">
        New analysis
      </Link>
      {/* Sign-out / account menu lives at the end of the topbar so it
          owns its own layout slot — no fixed-position overlay fighting
          with page buttons. */}
      <UserButton />
    </div>
  )
}
