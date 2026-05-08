import { createFileRoute } from '@tanstack/react-router'
import { useEffect, useState } from 'react'
import { api, getAuthToken } from '#/lib/api'
import type { RunDetail } from '#/lib/types'
import { AgentViewLive } from '#/components/flow/AgentViewLive'
import { ReadingMode } from '#/components/flow/ReadingMode'
import { Topbar } from '#/components/shared/Topbar'

export const Route = createFileRoute('/runs/$id')({
  loader: async ({ params }) => {
    /* Loaders run outside the React tree, so we can't gate this with
       <SignedIn>. Awaiting getAuthToken() forces Clerk to hydrate before
       the fetch goes out, which avoids a guaranteed 401 on first paint. */
    await getAuthToken()
    return { initial: await api.getRun(params.id) }
  },
  component: RunPage,
})

function RunPage() {
  const { initial } = Route.useLoaderData()
  const params = Route.useParams()
  const [run, setRun] = useState<RunDetail>(initial)

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
  return <ReadingMode run={run} />
}
