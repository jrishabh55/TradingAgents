import type { AgentStatus } from '#/lib/teams'

const MAP: Record<AgentStatus | 'interrupted', { cls: string; label: string }> = {
  done: { cls: 'ok', label: 'completed' },
  running: { cls: 'run', label: 'running' },
  queued: { cls: '', label: 'queued' },
  error: { cls: 'err', label: 'error' },
  interrupted: { cls: 'warn', label: 'interrupted' },
}

export function StatusBadge({
  status,
}: {
  status: AgentStatus | 'interrupted'
}) {
  const m = MAP[status]
  return (
    <span className={`es-pill ${m.cls}`}>
      <span className={`es-dot ${status === 'running' ? 'pulse' : ''}`} />
      {m.label}
    </span>
  )
}
