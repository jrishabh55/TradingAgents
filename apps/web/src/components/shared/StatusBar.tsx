export interface StatusBarProps {
  toolCalls?: number
  llmCalls?: number
  reports?: number
  tokens?: string | number
  cost?: string | number
  state?: 'live' | 'saved' | 'idle'
  rightLabel?: string
}

export function StatusBar({
  toolCalls = 0,
  llmCalls = 0,
  reports = 0,
  tokens = 0,
  cost = 0,
  state = 'idle',
  rightLabel,
}: StatusBarProps) {
  return (
    <div className="es-statusbar">
      <span>
        Tool Calls <b>{toolCalls}</b>
      </span>
      <span>
        LLM Calls <b>{llmCalls}</b>
      </span>
      <span>
        Reports <b>{reports}</b>
      </span>
      <span>
        Tokens <b>{tokens}</b>
      </span>
      <span>
        Cost <b>{typeof cost === 'number' ? `$${cost.toFixed(2)}` : cost}</b>
      </span>
      <div style={{ flex: 1 }} />
      <span>{rightLabel ?? (state === 'live' ? 'live' : state)}</span>
    </div>
  )
}
