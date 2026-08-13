/* Mirror of webapp/schemas.py — kept in sync by hand. The webapp1 backend is
   the source of truth; this file is what the frontend consumes. */

export type RunStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  /* Abandoned mid-flight by the server (e.g. process restart); resumable
     from its checkpoint via POST /runs/{id}/resume. */
  | 'interrupted'

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

/* One hit from GET /search/tickers — Yahoo Finance typeahead. */
export interface TickerHit {
  symbol: string
  name: string
  exchange: string
  type: string
}

export interface ProviderOption {
  key: string
  label: string
  backend_url?: string | null
  supports_reasoning_effort?: boolean
  supports_google_thinking?: boolean
  supports_anthropic_effort?: boolean
  /* Provider proxies through a helper process the user runs locally
     (e.g. `chatgpt_helper`). Absent/false for normal providers. */
  requires_helper?: boolean
  /* BYOC provider (Gemini): the user must bring their own credential —
     the UI checks GET /keys/gemini before allowing a run. */
  requires_user_key?: boolean
}

/* GET /keys/gemini — the user's Gemini credential state. `active_source`
   is what a run would use right now; null blocks submission. */
export interface GeminiKeyStatus {
  manual_key: boolean
  last4?: string | null
  oauth_available: boolean
  oauth_ok: boolean
  oauth_error?: string | null
  active_source: 'manual' | 'oauth' | null
}

/* GET /helper/status — whether the user's local helper is reachable. */
/* GET /me — activation + credits gate state. The middleware 403s
   non-activated users before this route, so a successful response always
   means activated. `credits: null` = gate off (no credit concept). */
export interface MeResponse {
  user_id: string
  activated: boolean
  credits: number | null
}

export interface HelperStatus {
  enabled: boolean
  mode: 'relay' | 'local' | null
  connected: boolean
  /* Where to fetch the helper package; '' when not configured. */
  download_url: string
  /* Version the connected helper reported; null/absent when disconnected or
     the build predates version reporting. */
  helper_version?: string | null
  /* True when the connected helper is older than the portal's tree (or too
     old to report a version) — drives the Topbar update nudge. */
  update_available?: boolean
}

/* POST /relay/pair — one-time pairing token (shown once) plus a
   ready-to-paste shell command that connects a local helper as this user. */
export interface PairResponse {
  token: string
  ws_url: string
  command: string
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

/* Computed risk levels — see apps/api/integrations/levels.py.
   Derived from OHLCV, not from the agents: the Trader asserts its own stop
   without price access, so `model_suggested` is shown for comparison only. */

export interface LevelValue {
  price: number
  /** The rule that produced this number, e.g. "20-bar swing low less 0.25x ATR". */
  basis: string
}

export interface ComputedLevels {
  entry: LevelValue
  stop: LevelValue
  target: LevelValue
  target_alt: LevelValue
  risk_per_share: number
  risk_pct_of_entry: number
  reward_risk_ratio: number
  resistance?: LevelValue | null
}

export interface PositionSize {
  shares: number
  cash_risk: number
  position_value: number
  capital: number
  risk_pct: number
  basis: string
}

export interface ModelSuggested {
  stop_loss?: number | null
  entry_price?: number | null
  position_sizing?: string | null
}

export interface LevelsResponse {
  run_id: string
  ticker: string
  analysis_date: string
  rating?: string | null
  /** Quote currency of the instrument. `capital` is assumed to be in it. */
  currency?: string | null
  viable: boolean
  viability_notes: string[]
  levels?: ComputedLevels | null
  size?: PositionSize | null
  model_suggested?: ModelSuggested | null
  divergence?: string | null
  disclaimer: string
}

export interface LevelsParams {
  capital: number
  risk_pct?: number
  r_multiple?: number
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
