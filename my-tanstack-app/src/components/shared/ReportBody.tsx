import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { StreamingMarkdown } from './StreamingMarkdown'

export interface ReportBodyProps {
  markdown?: string | null
  rating?: string | null
  ticker?: string
  /* compact: trim to first H2 — used by mid-run side-panels. */
  compact?: boolean
  /* When true, animate content updates with a typewriter pass. */
  streaming?: boolean
  /* Whether the source is still live (drives the trailing cursor). */
  live?: boolean
  /* Show a chrome-less variant (no padding, no header strip). */
  bare?: boolean
}

export function ReportBody({
  markdown,
  rating,
  ticker,
  compact = false,
  streaming = false,
  live = false,
  bare = false,
}: ReportBodyProps) {
  const empty = !markdown || markdown.trim().length === 0
  const padding = bare ? 0 : compact ? '16px 18px' : '22px 28px'

  if (empty) {
    return (
      <div className="es-report" style={{ padding }}>
        {live ? (
          <div className="streaming-banner">
            waiting for the first chunk…
          </div>
        ) : (
          <p style={{ color: 'var(--text-3)' }}>
            Report will stream in here as the agents finish.
          </p>
        )}
      </div>
    )
  }

  let body = markdown!
  if (compact) {
    const parts = body.split(/^## /m)
    if (parts.length > 2) body = parts.slice(0, 2).join('## ')
  }

  return (
    <div className="es-report" style={{ padding }}>
      {(rating || ticker) && (
        <div
          style={{
            display: 'flex',
            alignItems: 'baseline',
            gap: 12,
            marginBottom: 4,
            flexWrap: 'wrap',
          }}
        >
          {ticker && <h1>{ticker}</h1>}
          {rating && <span className="es-pill accent">{rating}</span>}
        </div>
      )}
      {streaming ? (
        <StreamingMarkdown content={body} live={live} />
      ) : (
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
      )}
    </div>
  )
}
