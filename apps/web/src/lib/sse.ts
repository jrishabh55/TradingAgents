import { useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { SseEvent, SseEventType } from './types'

export interface SseState {
  events: SseEvent[]
  status: 'idle' | 'connecting' | 'open' | 'closed'
  error: string | null
}

/* Subscribe to /api/runs/{id}/events. The browser's EventSource handles
   Last-Event-ID resume automatically when the connection drops, which matches
   webapp1's replay-from-SQLite contract. */
export function useRunEvents(runId: string | undefined): SseState {
  const [state, setState] = useState<SseState>({
    events: [],
    status: 'idle',
    error: null,
  })
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    if (!runId) return
    setState({ events: [], status: 'connecting', error: null })

    /* Same-origin GET — the browser attaches the Clerk session cookie
       automatically, so no token wiring is needed here. */
    const es = new EventSource(api.eventsUrl(runId))
    esRef.current = es
    attachHandlers(es)

    function attachHandlers(source: EventSource) {
      source.onopen = () =>
        setState((s) => ({ ...s, status: 'open', error: null }))

      /* The API emits each event with a typed `event:` line — listen on
         each known type. We push into a single ordered list so consumers
         can render a timeline. */
      const types: SseEventType[] = [
        'run.started',
        'analyst.started',
        'analyst.report',
        'analyst.completed',
        'team.started',
        'debate.update',
        'team.completed',
        'report.section',
        'tool.called',
        'heartbeat',
        'run.final',
        'run.failed',
        'run.cancelled',
      ]

      const handlers: Record<string, (ev: MessageEvent) => void> = {}
      for (const type of types) {
        const h = (ev: MessageEvent) => {
          if (type === 'heartbeat') return
          let data: Record<string, unknown> = {}
          try {
            data = ev.data ? JSON.parse(ev.data) : {}
          } catch {
            /* ignore malformed event data */
          }
          const seq = Number(ev.lastEventId ?? 0)
          setState((s) => ({
            ...s,
            events: [...s.events, { seq, type, data }],
          }))
        }
        handlers[type] = h
        source.addEventListener(type, h as EventListener)
      }

      source.onerror = () => {
        setState((s) => ({ ...s, status: 'closed', error: 'connection error' }))
      }

      /* Stash the cleanup on the source so the unmount return below can
         find it without closing over `types`/`handlers` separately. */
      ;(source as EventSource & { _cleanup?: () => void })._cleanup = () => {
        for (const type of types) {
          source.removeEventListener(type, handlers[type] as EventListener)
        }
      }
    }

    return () => {
      const current = esRef.current as
        | (EventSource & { _cleanup?: () => void })
        | null
      if (current) {
        current._cleanup?.()
        current.close()
      }
      esRef.current = null
    }
  }, [runId])

  return state
}
