import type { AgentStatus } from '#/lib/teams'

const MAP: Record<AgentStatus, { cls: string; label: string }> = {
  done: { cls: 'ok', label: 'completed' },
  running: { cls: 'run', label: 'running' },
  queued: { cls: '', label: 'queued' },
  error: { cls: 'err', label: 'error' },
}

export function StatusBadge({ status }: { status: AgentStatus }) {
  const m = MAP[status]
  return (
    <span className={`es-pill ${m.cls}`}>
      <span className={`es-dot ${status === 'running' ? 'pulse' : ''}`} />
      {m.label}
    </span>
  )
}
