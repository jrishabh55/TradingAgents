/* Mirror of webapp/schemas.py — kept in sync by hand. The webapp1 backend is
   the source of truth; this file is what the frontend consumes. */

export type RunStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface RunRequest {
  ticker: string
  analysis_date: string
  analysts: string[]
  research_depth: number
  llm_provider: string
  backend_url?: string | null
  shallow_thinker: string
  deep_thinker: string
  google_thinking_level?: string | null
  openai_reasoning_effort?: string | null
  anthropic_effort?: string | null
  output_language?: string
  checkpoint_enabled?: boolean
}

export interface RunSummary {
  id: string
  ticker: string
  analysis_date: string
  status: RunStatus
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  rating?: string | null
  error?: string | null
}

export interface RunDetail extends RunSummary {
  decision_text?: string | null
  config: Record<string, unknown>
  market_report?: string | null
  sentiment_report?: string | null
  news_report?: string | null
  fundamentals_report?: string | null
  investment_plan?: string | null
  trader_investment_plan?: string | null
  final_trade_decision?: string | null
  investment_debate_state?: Record<string, unknown> | null
  risk_debate_state?: Record<string, unknown> | null
}

export interface ProviderOption {
  key: string
  label: string
  backend_url?: string | null
  supports_reasoning_effort?: boolean
  supports_google_thinking?: boolean
  supports_anthropic_effort?: boolean
}

export interface ModelOption {
  id: string
  label: string
}

export interface ConfigResponse {
  analysts: { key: string; label: string }[]
  research_depths: { value: number; label: string }[]
  providers: ProviderOption[]
  models_by_provider: Record<string, ModelOption[]>
  output_languages: { value: string; label: string }[]
  default_ticker: string
}

/* SSE event taxonomy — see webapp/jobs/translator.py */
export type SseEventType =
  | 'run.started'
  | 'analyst.started'
  | 'analyst.report'
  | 'analyst.completed'
  | 'team.started'
  | 'debate.update'
  | 'team.completed'
  | 'report.section'
  | 'tool.called'
  | 'heartbeat'
  | 'run.final'
  | 'run.failed'
  | 'run.cancelled'

export interface SseEvent<T = Record<string, unknown>> {
  seq: number
  type: SseEventType
  data: T
}
