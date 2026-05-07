import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { api } from '#/lib/api'
import type {
  ConfigResponse,
  RunRequest,
  RunSummary,
} from '#/lib/types'
import { Topbar } from '#/components/shared/Topbar'
import { FlowAdvanced } from './FlowAdvanced'

const TICKER_SUFFIXES: { s: string; l: string }[] = [
  { s: '.NS', l: 'NSE' },
  { s: '.BO', l: 'BSE' },
  { s: '.TO', l: 'TSX' },
  { s: '.T', l: 'Tokyo' },
  { s: '.HK', l: 'Hong Kong' },
  { s: '.L', l: 'London' },
]

const ANALYSTS = [
  { id: 'market', name: 'Market', desc: 'Technicals, charts, indicators' },
  { id: 'social', name: 'Social', desc: 'Reddit, X, sentiment' },
  { id: 'news', name: 'News', desc: 'Headlines, macro events' },
  { id: 'fundamentals', name: 'Fundamentals', desc: 'Earnings, ratios, filings' },
]

const DEPTHS = [
  { value: 1, label: 'Shallow', meta: '1 round' },
  { value: 2, label: 'Standard', meta: '2 rounds' },
  { value: 4, label: 'Deep', meta: '4 rounds' },
]

const VERDICT_CLS: Record<string, string> = {
  'STRONG BUY': 'ok',
  BUY: 'ok',
  HOLD: 'info',
  REDUCE: 'run',
  SELL: 'err',
  'STRONG SELL': 'err',
}

export interface FlowState {
  ticker: string
  date: string
  depth: number
  analysts: string[]
  provider: string
  shallowThinker: string
  deepThinker: string
  language: string
  reasoningEffort?: string
  backendUrl?: string
  checkpoint: boolean
}

export function FlowLanding() {
  const navigate = useNavigate()
  const [config, setConfig] = useState<ConfigResponse | null>(null)
  const [recents, setRecents] = useState<RunSummary[]>([])
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const today = useMemo(() => new Date().toISOString().slice(0, 10), [])
  const [form, setForm] = useState<FlowState>({
    ticker: 'SPY',
    date: today,
    depth: 1,
    analysts: ['market', 'social', 'news', 'fundamentals'],
    provider: 'openai',
    shallowThinker: '',
    deepThinker: '',
    language: 'English',
    checkpoint: false,
  })

  useEffect(() => {
    api
      .config()
      .then((c) => {
        setConfig(c)
        setForm((f) => {
          const provider = c.providers[0]?.key ?? f.provider
          const models = c.models_by_provider[provider] ?? []
          return {
            ...f,
            ticker: c.default_ticker || f.ticker,
            provider,
            shallowThinker: f.shallowThinker || models[0]?.id || '',
            deepThinker:
              f.deepThinker || models[1]?.id || models[0]?.id || '',
          }
        })
      })
      .catch((e: Error) => setError(`Backend unreachable: ${e.message}`))

    api
      .listRuns()
      .then((rs) => setRecents(rs.slice(0, 6)))
      .catch(() => {
        /* ignored — recents are best-effort */
      })
  }, [])

  function patch(p: Partial<FlowState>) {
    setForm((f) => ({ ...f, ...p }))
  }

  function toggleAnalyst(id: string) {
    setForm((f) => ({
      ...f,
      analysts: f.analysts.includes(id)
        ? f.analysts.filter((a) => a !== id)
        : [...f.analysts, id],
    }))
  }

  async function startRun() {
    if (!config) return
    setSubmitting(true)
    setError(null)
    const provider = config.providers.find((p) => p.key === form.provider)
    const body: RunRequest = {
      ticker: form.ticker.trim().toUpperCase(),
      analysis_date: form.date,
      analysts: form.analysts,
      research_depth: form.depth,
      llm_provider: form.provider,
      backend_url: form.backendUrl || provider?.backend_url || null,
      shallow_thinker: form.shallowThinker,
      deep_thinker: form.deepThinker,
      output_language: form.language,
      checkpoint_enabled: form.checkpoint,
      openai_reasoning_effort: provider?.supports_reasoning_effort
        ? form.reasoningEffort
        : undefined,
      google_thinking_level: provider?.supports_google_thinking
        ? form.reasoningEffort
        : undefined,
      anthropic_effort: provider?.supports_anthropic_effort
        ? form.reasoningEffort
        : undefined,
    }
    try {
      const detail = await api.createRun(body)
      navigate({ to: '/runs/$id', params: { id: detail.id } })
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
      setSubmitting(false)
    }
  }

  const analystSummary =
    form.analysts.length === ANALYSTS.length
      ? `All ${ANALYSTS.length} selected`
      : `${form.analysts.length} of ${ANALYSTS.length}`

  const providerLabel =
    config?.providers.find((p) => p.key === form.provider)?.label ?? form.provider
  const shallowLabel =
    config?.models_by_provider[form.provider]?.find(
      (m) => m.id === form.shallowThinker,
    )?.label ?? form.shallowThinker

  return (
    <div className="es-art">
      <Topbar state="idle" />

      {showAdvanced ? (
        <FlowAdvanced
          form={form}
          patch={patch}
          toggleAnalyst={toggleAnalyst}
          config={config}
          submitting={submitting}
          onClose={() => setShowAdvanced(false)}
          onStart={startRun}
        />
      ) : (
        <div style={{ flex: 1, overflow: 'auto', padding: '60px 24px' }}>
          <div style={{ maxWidth: 760, margin: '0 auto' }}>
            <div style={{ textAlign: 'center', marginBottom: 30 }}>
              <div
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  letterSpacing: '0.12em',
                  textTransform: 'uppercase',
                  color: 'var(--accent-hi)',
                  marginBottom: 8,
                }}
              >
                ▸ Run a new analysis
              </div>
              <div
                style={{
                  fontSize: 32,
                  fontWeight: 600,
                  letterSpacing: '-0.02em',
                  marginBottom: 8,
                }}
              >
                What are we analyzing?
              </div>
              <div style={{ fontSize: 14, color: 'var(--text-3)' }}>
                Type a ticker. Defaults are tuned for a quick, balanced run.
              </div>
            </div>

            {/* hero command bar */}
            <div
              style={{
                background: 'var(--bg-1)',
                border: '1px solid var(--accent-line)',
                borderRadius: 16,
                padding: 6,
                boxShadow:
                  '0 0 0 4px rgba(79,124,255,0.08), 0 12px 40px rgba(0,0,0,0.4)',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 0 }}>
                <div
                  style={{
                    paddingLeft: 18,
                    color: 'var(--text-4)',
                    fontSize: 18,
                    fontFamily: 'var(--font-mono)',
                  }}
                >
                  $
                </div>
                <input
                  value={form.ticker}
                  onChange={(e) => patch({ ticker: e.target.value })}
                  spellCheck={false}
                  style={{
                    flex: 1,
                    background: 'transparent',
                    border: 0,
                    outline: 'none',
                    padding: '20px 14px',
                    fontSize: 26,
                    fontWeight: 600,
                    color: 'var(--text-1)',
                    fontFamily: 'var(--font-mono)',
                    letterSpacing: '0.02em',
                  }}
                />
                <input
                  type="date"
                  value={form.date}
                  onChange={(e) => patch({ date: e.target.value })}
                  style={{
                    background: 'var(--bg-2)',
                    border: '1px solid var(--line-2)',
                    color: 'var(--text-2)',
                    padding: '10px 12px',
                    borderRadius: 10,
                    marginRight: 10,
                    fontSize: 13,
                    colorScheme: 'dark',
                  }}
                />
                <button
                  className="es-btn primary"
                  style={{
                    height: 50,
                    padding: '0 24px',
                    fontSize: 14,
                    marginRight: 6,
                  }}
                  onClick={startRun}
                  disabled={submitting || form.analysts.length === 0}
                >
                  {submitting ? 'Starting…' : 'Start analysis →'}
                </button>
              </div>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '10px 18px',
                  borderTop: '1px solid var(--line-1)',
                  fontSize: 11.5,
                  color: 'var(--text-3)',
                  flexWrap: 'wrap',
                }}
              >
                <span>Suffixes:</span>
                {TICKER_SUFFIXES.map((t) => (
                  <span
                    key={t.s}
                    style={{
                      padding: '2px 7px',
                      borderRadius: 4,
                      background: 'var(--bg-2)',
                      border: '1px solid var(--line-1)',
                      fontFamily: 'var(--font-mono)',
                      fontSize: 11,
                    }}
                  >
                    {t.s}{' '}
                    <span style={{ color: 'var(--text-4)' }}>{t.l}</span>
                  </span>
                ))}
              </div>
            </div>

            {/* labelled chip groups */}
            <div
              style={{
                display: 'flex',
                flexWrap: 'wrap',
                gap: 16,
                rowGap: 12,
                marginTop: 20,
                justifyContent: 'center',
                alignItems: 'center',
              }}
            >
              <ChipGroup label="Depth">
                {DEPTHS.map((d) => (
                  <button
                    key={d.value}
                    className={`chip ${form.depth === d.value ? 'active' : ''}`}
                    onClick={() => patch({ depth: d.value })}
                  >
                    {d.label} · {d.meta}
                  </button>
                ))}
              </ChipGroup>
              <span
                style={{
                  width: 1,
                  height: 18,
                  background: 'var(--line-2)',
                }}
              />
              <ChipGroup label="Analysts">
                <button
                  className="chip active"
                  onClick={() => setShowAdvanced(true)}
                  title="Open advanced settings"
                >
                  {analystSummary}
                </button>
              </ChipGroup>
              <ChipGroup label="Model">
                <button
                  className="chip"
                  onClick={() => setShowAdvanced(true)}
                  title="Open advanced settings"
                >
                  {providerLabel} · {shallowLabel || '—'}
                </button>
              </ChipGroup>
              <ChipGroup label="Language">
                <button
                  className="chip"
                  onClick={() => setShowAdvanced(true)}
                >
                  {form.language}
                </button>
              </ChipGroup>
              <button
                className="chip ghost"
                onClick={() => setShowAdvanced(true)}
              >
                + Advanced
              </button>
            </div>

            {error && (
              <div
                style={{
                  marginTop: 18,
                  padding: '10px 14px',
                  background: 'var(--err-bg)',
                  border: '1px solid rgba(239,79,79,0.3)',
                  borderRadius: 10,
                  fontSize: 12.5,
                  color: 'var(--err)',
                }}
              >
                {error}
              </div>
            )}

            {/* recents */}
            {recents.length > 0 && (
              <div style={{ marginTop: 38 }}>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'baseline',
                    marginBottom: 10,
                  }}
                >
                  <div className="es-team-label">Recent runs</div>
                  <div style={{ flex: 1 }} />
                </div>
                <div className="es-card">
                  {recents.map((r, i) => {
                    const verdictCls =
                      (r.rating && VERDICT_CLS[r.rating.toUpperCase()]) ||
                      'info'
                    const finished = r.status === 'completed'
                    return (
                      <div
                        key={r.id}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 14,
                          padding: '12px 18px',
                          borderBottom:
                            i < recents.length - 1
                              ? '1px solid var(--line-1)'
                              : 'none',
                          cursor: 'pointer',
                        }}
                        onClick={() =>
                          navigate({
                            to: '/runs/$id',
                            params: { id: r.id },
                          })
                        }
                      >
                        <div
                          className="es-mono"
                          style={{
                            fontSize: 14,
                            fontWeight: 600,
                            width: 100,
                          }}
                        >
                          {r.ticker}
                        </div>
                        <div
                          style={{
                            fontSize: 12,
                            color: 'var(--text-3)',
                            width: 130,
                          }}
                        >
                          {r.analysis_date}
                        </div>
                        <span
                          className={`es-pill ${finished ? verdictCls : 'run'}`}
                          style={{ fontSize: 11 }}
                        >
                          {finished
                            ? r.rating || r.status
                            : r.status}
                        </span>
                        <div style={{ flex: 1 }} />
                        <button className="es-btn ghost sm">Open</button>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function ChipGroup({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
      }}
    >
      <span
        style={{
          fontSize: 10.5,
          fontWeight: 600,
          letterSpacing: '0.08em',
          textTransform: 'uppercase',
          color: 'var(--text-4)',
          marginRight: 2,
        }}
      >
        {label}
      </span>
      {children}
    </div>
  )
}
