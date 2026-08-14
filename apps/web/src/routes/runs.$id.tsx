import { createFileRoute } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { api } from '#/lib/api'
import type { RunDetail } from '#/lib/types'
import { AgentViewLive } from '#/components/flow/AgentViewLive'
import { ReadingMode } from '#/components/flow/ReadingMode'
import { Topbar } from '#/components/shared/Topbar'

export const Route = createFileRoute('/runs/$id')({
  loader: async ({ params }) => {
    return { initial: await api.getRun(params.id) }
  },
  component: RunPage,
})

function RunPage() {
  const { initial } = Route.useLoaderData()
  const params = Route.useParams()
  const [run, setRun] = useState<RunDetail>(initial)
  const [resuming, setResuming] = useState(false)
  const [resumeError, setResumeError] = useState<string | null>(null)

  /* Poll the detail endpoint while the run is active so we can flip from
     AgentViewLive to ReadingMode when status moves to completed. SSE handles
     fine-grained timeline updates; this loop just refreshes status + the
     persisted reports. */
  useEffect(() => {
    if (run.status !== 'queued' && run.status !== 'running') return
    let stopped = false
    const tick = async () => {
      try {
        const next = await api.getRun(params.id)
        if (!stopped) setRun(next)
      } catch {
        /* SSE keeps the agent timeline live regardless */
      }
    }
    const t = setInterval(tick, 4000)
    return () => {
      stopped = true
      clearInterval(t)
    }
  }, [params.id, run.status])

  /* Resume an interrupted run from its checkpoint. On success the returned
     RunDetail carries the new status (back to queued/running), which also
     restarts the polling effect above. A 409's `detail` is the human-readable
     reason (no checkpoint, already resuming, limit hit) — show it as-is. */
  async function resume() {
    setResuming(true)
    setResumeError(null)
    try {
      const next = await api.resumeRun(params.id)
      setRun(next)
    } catch (e: unknown) {
      setResumeError(e instanceof Error ? e.message : String(e))
    } finally {
      setResuming(false)
    }
  }

  if (run.status === 'queued') {
    return (
      <div className="es-art">
        <Topbar ticker={run.ticker} date={run.analysis_date} state="running" />
        <div
          style={{
            flex: 1,
            display: 'grid',
            placeItems: 'center',
            color: 'var(--text-3)',
          }}
        >
          Waiting for a worker slot…
        </div>
      </div>
    )
  }

  if (run.status === 'running') return <AgentViewLive run={run} />

  /* An interrupted run has no finished report — offer resume instead of
     dropping into ReadingMode. */
  if (run.status === 'interrupted') {
    return (
      <div className="es-art">
        <Topbar
          ticker={run.ticker}
          date={run.analysis_date}
          state="interrupted"
        />
        <div
          style={{
            flex: 1,
            display: 'grid',
            placeItems: 'center',
            padding: 24,
          }}
        >
          <div
            className="es-card"
            style={{
              maxWidth: 460,
              padding: '26px 28px',
              textAlign: 'center',
              display: 'grid',
              gap: 12,
              justifyItems: 'center',
            }}
          >
            <span className="es-pill warn">
              <span className="es-dot" />
              interrupted
            </span>
            <div style={{ fontSize: 15, fontWeight: 600 }}>
              This run was interrupted before finishing
            </div>
            <div style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
              The server abandoned it mid-flight (e.g. a restart). You can
              resume from the last checkpoint.
            </div>
            {resumeError && (
              <div
                style={{
                  padding: '8px 12px',
                  background: 'var(--err-bg)',
                  border: '1px solid rgba(239,79,79,0.3)',
                  borderRadius: 10,
                  fontSize: 12.5,
                  color: 'var(--err)',
                }}
              >
                {resumeError}
              </div>
            )}
            <button
              className="es-btn primary"
              onClick={resume}
              disabled={resuming}
            >
              {resuming ? 'Resuming…' : 'Resume run'}
            </button>
          </div>
        </div>
      </div>
    )
  }

  return <ReadingMode run={run} />
}
