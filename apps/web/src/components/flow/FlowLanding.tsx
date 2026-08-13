import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { api, noMarketDataDetail } from '#/lib/api'
import { ratingTooltip } from '#/lib/rating'
import type {
  ConfigResponse,
  GeminiKeyStatus,
  HelperStatus,
  PairResponse,
  RunRequest,
  RunSummary,
  TickerHit,
} from '#/lib/types'
import { Topbar } from '#/components/shared/Topbar'
/* Raw cmdk input: ui/command's CommandInput carries search-box chrome that
   doesn't fit the hero bar; the primitive takes the hero's own styling. */
import { Command as CommandPrimitive } from 'cmdk'
import {
  Command,
  CommandEmpty,
  CommandItem,
  CommandList,
} from '#/components/ui/command'
import { FlowAdvanced } from './FlowAdvanced'
import { GeminiSetup } from './GeminiSetup'

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
  checkpoint: boolean
}

export function FlowLanding() {
  const navigate = useNavigate()
  const [config, setConfig] = useState<ConfigResponse | null>(null)
  const [recents, setRecents] = useState<RunSummary[]>([])
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /* Symbols the backend verified DO have data, when the submitted one didn't.
     Offered as one-tap fixes (NSEI → ^NSEI). */
  const [tickerSuggestions, setTickerSuggestions] = useState<string[]>([])
  /* Fetched once when a `requires_helper` provider is selected; null while
     no such provider is active (or the fetch is in flight). */
  const [helperStatus, setHelperStatus] = useState<HelperStatus | null>(null)
  /* Same idea for `requires_user_key` (Gemini BYOC). `geminiRefresh` bumps to
     refetch after a key save/remove. */
  const [geminiStatus, setGeminiStatus] = useState<GeminiKeyStatus | null>(null)
  const [geminiRefresh, setGeminiRefresh] = useState(0)
  /* Yahoo typeahead: only open after the user actually types (not on mount
     with the default ticker). Keyboard nav and row highlight come from cmdk. */
  const [hits, setHits] = useState<TickerHit[]>([])
  const [searchOpen, setSearchOpen] = useState(false)
  /* True only after a response for the CURRENT query came back empty —
     never while a fetch is still in flight, so "no matches" can't flash
     before results arrive. */
  const [noResults, setNoResults] = useState(false)

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
    checkpoint: true,
  })

  useEffect(() => {
    api
      .config()
      .then((c) => {
        setConfig(c)
        /* The helper-backed provider is always listed first, even with no
           helper installed — default to the first provider that works
           without one, and only auto-select the helper below once the
           probe confirms it is actually connected. */
        const fallback =
          c.providers.find((p) => !p.requires_helper) ?? c.providers[0]
        setForm((f) => {
          const provider = fallback?.key ?? f.provider
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
        const helper = c.providers.find((p) => p.requires_helper)
        if (!helper) return
        api
          .getHelperStatus()
          .then((s) => {
            if (!s.connected) return
            /* Zero-config path: the user's helper is already linked, so make
               it the active provider — unless they changed providers while
               the probe was in flight. */
            setForm((f) => {
              if (f.provider !== fallback?.key) return f
              const ms = c.models_by_provider[helper.key] ?? []
              return {
                ...f,
                provider: helper.key,
                shallowThinker: ms[0]?.id ?? '',
                deepThinker: ms[1]?.id ?? ms[0]?.id ?? '',
              }
            })
          })
          .catch(() => {
            /* ignored — auto-select is best-effort */
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

  const selectedProvider = config?.providers.find(
    (p) => p.key === form.provider,
  )

  /* A helper-backed provider with no reachable helper would start a run that
     is guaranteed to fail deep in the pipeline — block submission until the
     probe confirms it (null = probe still in flight, also blocked). */
  const helperBlocked =
    !!selectedProvider?.requires_helper && helperStatus?.connected !== true

  /* A BYOC provider (Gemini) with no resolvable credential fails at submit —
     block until the status probe reports a usable source (null = in flight,
     also blocked). */
  const geminiBlocked =
    !!selectedProvider?.requires_user_key && geminiStatus?.active_source == null

  /* Fetch the Gemini credential state when a BYOC provider is selected;
     `geminiRefresh` re-runs this after GeminiSetup saves/removes a key. */
  useEffect(() => {
    setGeminiStatus(null)
    if (!selectedProvider?.requires_user_key) return
    let stale = false
    api
      .getGeminiKeyStatus()
      .then((s) => {
        if (!stale) setGeminiStatus(s)
      })
      .catch(() => {
        if (!stale)
          setGeminiStatus({
            manual_key: false,
            oauth_available: false,
            oauth_ok: false,
            active_source: null,
          })
      })
    return () => {
      stale = true
    }
  }, [selectedProvider?.requires_user_key, form.provider, geminiRefresh])

  /* Check helper reachability when a helper-backed provider is selected,
     then re-probe every 3s until it connects — the setup card's "waiting"
     line goes live and the `helperBlocked` gate opens without a refresh.
     An unreachable endpoint reads the same as a stopped helper. */
  useEffect(() => {
    setHelperStatus(null)
    if (!selectedProvider?.requires_helper) return
    let stale = false
    let timer: ReturnType<typeof setInterval> | undefined
    const probe = () => {
      api
        .getHelperStatus()
        .then((s) => {
          if (stale) return
          setHelperStatus(s)
          if (s.connected && timer !== undefined) clearInterval(timer)
        })
        .catch(() => {
          if (!stale)
            setHelperStatus({
              enabled: false,
              mode: null,
              connected: false,
              download_url: '',
            })
        })
    }
    probe()
    timer = setInterval(probe, 3000)
    return () => {
      stale = true
      clearInterval(timer)
    }
  }, [selectedProvider?.requires_helper, form.provider])

  function patch(p: Partial<FlowState>) {
    setForm((f) => ({ ...f, ...p }))
  }

  /* Debounced ticker search. Re-runs per keystroke; cleanup marks the
     in-flight request stale so a slow earlier response can't clobber a
     newer one. */
  useEffect(() => {
    if (!searchOpen) return
    const q = form.ticker.trim()
    if (q.length < 2) {
      setHits([])
      setNoResults(false)
      return
    }
    let stale = false
    const t = setTimeout(() => {
      api
        .searchTickers(q)
        .then((r) => {
          if (stale) return
          setHits(r.results)
          setNoResults(r.results.length === 0)
        })
        .catch(() => {
          /* API/Yahoo failure ≠ "this ticker doesn't exist" — stay silent. */
          if (!stale) setHits([])
        })
    }, 250)
    return () => {
      stale = true
      clearTimeout(t)
    }
  }, [form.ticker, searchOpen])

  function pickHit(hit: TickerHit) {
    patch({ ticker: hit.symbol })
    setSearchOpen(false)
    setHits([])
    setNoResults(false)
  }

  function toggleAnalyst(id: string) {
    setForm((f) => ({
      ...f,
      analysts: f.analysts.includes(id)
        ? f.analysts.filter((a) => a !== id)
        : [...f.analysts, id],
    }))
  }

  async function startRun(tickerOverride?: string) {
    if (!config || helperBlocked || geminiBlocked) return
    setSubmitting(true)
    setError(null)
    setTickerSuggestions([])
    const provider = config.providers.find((p) => p.key === form.provider)
    const body: RunRequest = {
      ticker: (tickerOverride ?? form.ticker).trim().toUpperCase(),
      analysis_date: form.date,
      analysts: form.analysts,
      research_depth: form.depth,
      llm_provider: form.provider,
      /* The server dictates each provider's endpoint and ignores this field;
         null keeps the cache key stable across clients. */
      backend_url: null,
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
      /* A ticker with no price data is rejected before the pipeline starts, so
         no tokens were spent — show the verified alternatives instead of a
         bare error. */
      const noData = noMarketDataDetail(e)
      if (noData) {
        setError(noData.message)
        setTickerSuggestions(noData.suggestions)
      } else {
        setError(e instanceof Error ? e.message : String(e))
      }
      setSubmitting(false)
    }
  }

  /** Adopt a suggested symbol and immediately retry with it. */
  function useSuggestion(symbol: string) {
    patch({ ticker: symbol })
    void startRun(symbol)
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
          helperStatus={helperStatus}
          geminiStatus={geminiStatus}
          onGeminiChanged={() => setGeminiRefresh((n) => n + 1)}
          onClose={() => setShowAdvanced(false)}
          /* Same reason as the button below — FlowAdvanced wires this straight
             to onClick, so an unwrapped startRun would receive the event. */
          onStart={() => startRun()}
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
              <Command
                shouldFilter={false}
                loop
                className="overflow-visible rounded-none bg-transparent p-0"
                style={{
                  display: 'flex',
                  flexDirection: 'row',
                  alignItems: 'center',
                  gap: 0,
                  position: 'relative',
                }}
              >
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
                <CommandPrimitive.Input
                  value={form.ticker}
                  onValueChange={(v) => {
                    patch({ ticker: v })
                    setSearchOpen(true)
                    setNoResults(false)
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') setSearchOpen(false)
                  }}
                  onBlur={() => setSearchOpen(false)}
                  placeholder="Ticker or company name"
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
                  /* Wrapped, not passed directly: React would hand the
                     MouseEvent to startRun's tickerOverride parameter. */
                  onClick={() => startRun()}
                  disabled={
                    submitting ||
                    helperBlocked ||
                    geminiBlocked ||
                    form.analysts.length === 0
                  }
                >
                  {submitting ? 'Starting…' : 'Start analysis →'}
                </button>
                {searchOpen && (hits.length > 0 || noResults) && (
                  <CommandList
                    style={{
                      position: 'absolute',
                      top: '100%',
                      left: 0,
                      right: 0,
                      zIndex: 20,
                      marginTop: 4,
                      background: 'var(--bg-1)',
                      border: '1px solid var(--line-2)',
                      borderRadius: 12,
                      boxShadow: '0 12px 40px rgba(0,0,0,0.5)',
                    }}
                  >
                    <CommandEmpty style={{ color: 'var(--text-3)' }}>
                      No matches on Yahoo Finance. If you know the symbol,
                      type it exactly — with its exchange suffix for non-US
                      listings (e.g. RELIANCE.NS).
                    </CommandEmpty>
                    {hits.map((h) => (
                      <CommandItem
                        key={h.symbol}
                        value={h.symbol}
                        onSelect={() => pickHit(h)}
                        /* Keep focus in the input: without this, blur closes
                           the list before the click lands. */
                        onMouseDown={(e) => e.preventDefault()}
                        className="cursor-pointer gap-2.5 px-4 py-2.5"
                      >
                        <span
                          className="es-mono"
                          style={{ fontWeight: 600, minWidth: 90 }}
                        >
                          {h.symbol}
                        </span>
                        <span
                          style={{
                            flex: 1,
                            color: 'var(--text-3)',
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                          }}
                        >
                          {h.name}
                        </span>
                        <span
                          style={{ fontSize: 11, color: 'var(--text-4)' }}
                        >
                          {h.exchange}
                          {h.type && h.type !== 'EQUITY'
                            ? ` · ${h.type.toLowerCase()}`
                            : ''}
                        </span>
                      </CommandItem>
                    ))}
                  </CommandList>
                )}
              </Command>
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

            {selectedProvider?.requires_helper && helperStatus && (
              <div
                style={{
                  marginTop: 12,
                  display: 'flex',
                  justifyContent: 'center',
                }}
              >
                <HelperSetup status={helperStatus} />
              </div>
            )}

            {selectedProvider?.requires_user_key && (
              <div
                style={{
                  marginTop: 12,
                  display: 'flex',
                  justifyContent: 'center',
                }}
              >
                <GeminiSetup
                  status={geminiStatus}
                  onChanged={() => setGeminiRefresh((n) => n + 1)}
                />
              </div>
            )}

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
                {tickerSuggestions.length > 0 && (
                  <div
                    style={{
                      marginTop: 10,
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      flexWrap: 'wrap',
                    }}
                  >
                    <span style={{ color: 'var(--text-3)' }}>Try:</span>
                    {tickerSuggestions.map((s) => (
                      <button
                        key={s}
                        type="button"
                        className="es-btn sm es-mono"
                        disabled={submitting}
                        onClick={() => useSuggestion(s)}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                )}
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
                          className={`es-pill ${
                            finished
                              ? verdictCls
                              : r.status === 'interrupted'
                                ? 'warn'
                                : 'run'
                          }`}
                          style={{ fontSize: 11 }}
                          title={finished ? ratingTooltip(r.rating) : undefined}
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

/** Helper reachability + onboarding, shown when the selected provider has
 *  `requires_helper`. Connected → subtle confirmation; otherwise a compact
 *  setup card walking through install → pair → wait. Shared by the compact
 *  landing and FlowAdvanced. Polling stays with FlowLanding — this only
 *  renders whatever `status` it is handed; the pair-button state is local. */
export function HelperSetup({ status }: { status: HelperStatus | null }) {
  const [pair, setPair] = useState<PairResponse | null>(null)
  const [pairing, setPairing] = useState(false)
  const [pairError, setPairError] = useState<string | null>(null)
  const [copied, setCopied] = useState<string | null>(null)

  if (!status) return null
  if (status.connected) {
    return (
      <div style={{ fontSize: 11.5, color: 'var(--ok)' }}>
        Helper connected{status.mode ? ` · ${status.mode}` : ''}
      </div>
    )
  }

  async function generateCode() {
    setPairing(true)
    setPairError(null)
    try {
      setPair(await api.pairHelper())
    } catch (e: unknown) {
      setPairError(e instanceof Error ? e.message : String(e))
    } finally {
      setPairing(false)
    }
  }

  function copyValue(key: string, text: string) {
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(key)
      setTimeout(() => setCopied(null), 1500)
    })
  }

  return (
    <div
      style={{
        width: '100%',
        maxWidth: 560,
        textAlign: 'left',
        background: 'var(--bg-1)',
        border: '1px solid rgba(251,146,60,0.35)',
        borderRadius: 12,
        padding: '14px 16px',
        display: 'grid',
        gap: 10,
        fontSize: 12,
      }}
    >
      <div style={{ fontSize: 12.5, fontWeight: 600, color: '#fb923c' }}>
        Local helper not connected
      </div>
      <div style={{ color: 'var(--text-3)' }}>
        This provider runs through the Drishti Helper app on your
        machine. Three steps:
      </div>

      <div style={{ display: 'grid', gap: 6 }}>
        <div style={{ fontWeight: 500, color: 'var(--text-2)' }}>
          1. Download and open the Drishti Helper app
        </div>
        {status.download_url ? (
          <a
            className="es-btn sm"
            style={{ justifySelf: 'start', textDecoration: 'none' }}
            href={status.download_url}
            target="_blank"
            rel="noreferrer"
          >
            Download the helper ↗
          </a>
        ) : (
          <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
            Ask your administrator for the helper app download.
          </div>
        )}
        <details>
          <summary
            style={{ fontSize: 11, color: 'var(--text-3)', cursor: 'pointer' }}
          >
            Developer setup (run from source)
          </summary>
          <div style={{ marginTop: 6 }}>
            <CodeBlock
              text={
                'uv pip install -r apps/helper/requirements.txt\npython -m apps.helper login'
              }
            />
          </div>
        </details>
      </div>

      <div style={{ display: 'grid', gap: 6 }}>
        <div style={{ fontWeight: 500, color: 'var(--text-2)' }}>
          2. Generate a connect code
        </div>
        {pair ? (
          <>
            <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
              Portal address
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
              <CodeBlock text={pair.ws_url} />
              <button
                className="es-btn sm"
                onClick={() => copyValue('ws_url', pair.ws_url)}
              >
                {copied === 'ws_url' ? 'Copied' : 'Copy'}
              </button>
            </div>
            <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
              Connect code
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
              <CodeBlock text={pair.token} />
              <button
                className="es-btn sm"
                onClick={() => copyValue('token', pair.token)}
              >
                {copied === 'token' ? 'Copied' : 'Copy'}
              </button>
            </div>
            <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
              Keep this code private — it lets a machine run analyses as you.
            </div>
            <details>
              <summary
                style={{
                  fontSize: 11,
                  color: 'var(--text-3)',
                  cursor: 'pointer',
                }}
              >
                Or connect from a terminal
              </summary>
              <div
                style={{
                  display: 'flex',
                  gap: 8,
                  alignItems: 'flex-start',
                  marginTop: 6,
                }}
              >
                <CodeBlock text={pair.command} />
                <button
                  className="es-btn sm"
                  onClick={() => copyValue('command', pair.command)}
                >
                  {copied === 'command' ? 'Copied' : 'Copy'}
                </button>
              </div>
            </details>
          </>
        ) : (
          <button
            className="es-btn sm"
            style={{ justifySelf: 'start' }}
            onClick={() => void generateCode()}
            disabled={pairing}
          >
            {pairing ? 'Generating…' : 'Generate connect code'}
          </button>
        )}
        {pairError && (
          <div style={{ fontSize: 11.5, color: 'var(--err)' }}>{pairError}</div>
        )}
      </div>

      <div style={{ display: 'grid', gap: 6 }}>
        <div style={{ fontWeight: 500, color: 'var(--text-2)' }}>
          3. Paste both into the helper app
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
          Paste both into the helper app's "Web portal connection" section —
          this page will update automatically.
        </div>
      </div>

      <div style={{ fontSize: 11.5, color: '#fb923c' }}>
        ● Waiting for your helper to connect… this updates automatically.
      </div>
    </div>
  )
}

function CodeBlock({ text }: { text: string }) {
  return (
    <pre
      className="es-mono"
      style={{
        flex: 1,
        margin: 0,
        padding: '8px 10px',
        background: 'var(--bg-2)',
        border: '1px solid var(--line-1)',
        borderRadius: 8,
        fontSize: 11,
        lineHeight: 1.6,
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-all',
        color: 'var(--text-2)',
      }}
    >
      {text}
    </pre>
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
