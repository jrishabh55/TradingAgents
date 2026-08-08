import type { ConfigResponse, HelperStatus } from '#/lib/types'
import type { FlowState } from './FlowLanding'
import { HelperSetup } from './FlowLanding'

const ANALYSTS = [
  { id: 'market', name: 'Market', desc: 'Technicals, charts, indicators' },
  { id: 'social', name: 'Social', desc: 'Reddit, X, sentiment' },
  { id: 'news', name: 'News', desc: 'Headlines, macro events' },
  {
    id: 'fundamentals',
    name: 'Fundamentals',
    desc: 'Earnings, ratios, filings',
  },
]

const DEPTHS = [
  {
    value: 1,
    title: 'Shallow',
    rounds: '1 round',
    time: '~6 min',
    cost: '$1.20',
  },
  {
    value: 2,
    title: 'Standard',
    rounds: '2 rounds',
    time: '~12 min',
    cost: '$2.40',
  },
  {
    value: 4,
    title: 'Deep',
    rounds: '4 rounds',
    time: '~28 min',
    cost: '$5.80',
  },
]

const REASONING = ['auto', 'low', 'medium', 'high']

export interface FlowAdvancedProps {
  form: FlowState
  patch: (p: Partial<FlowState>) => void
  toggleAnalyst: (id: string) => void
  config: ConfigResponse | null
  submitting: boolean
  /* Reachability of the local helper — only fetched (by FlowLanding) when
     the selected provider has `requires_helper`. */
  helperStatus: HelperStatus | null
  onClose: () => void
  onStart: () => void
}

export function FlowAdvanced({
  form,
  patch,
  toggleAnalyst,
  config,
  submitting,
  helperStatus,
  onClose,
  onStart,
}: FlowAdvancedProps) {
  const provider = config?.providers.find((p) => p.key === form.provider)
  /* Mirrors FlowLanding's gate: a helper-backed provider with no reachable
     helper must not submit a run that is guaranteed to fail. */
  const helperBlocked =
    !!provider?.requires_helper && helperStatus?.connected !== true
  const models = config?.models_by_provider[form.provider] ?? []

  const supportsReasoning =
    provider?.supports_reasoning_effort ||
    provider?.supports_google_thinking ||
    provider?.supports_anthropic_effort

  return (
    <div style={{ flex: 1, overflow: 'auto', padding: '44px 24px 60px' }}>
      <div style={{ maxWidth: 760, margin: '0 auto' }}>
        <div style={{ textAlign: 'center', marginBottom: 22 }}>
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
              fontSize: 26,
              fontWeight: 600,
              letterSpacing: '-0.02em',
            }}
          >
            What are we analyzing?
          </div>
        </div>

        {/* compact hero */}
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
          <div style={{ display: 'flex', alignItems: 'center' }}>
            <div
              style={{
                paddingLeft: 18,
                color: 'var(--text-4)',
                fontSize: 16,
                fontFamily: 'var(--font-mono)',
              }}
            >
              $
            </div>
            <input
              value={form.ticker}
              onChange={(e) => patch({ ticker: e.target.value })}
              style={{
                flex: 1,
                background: 'transparent',
                border: 0,
                outline: 'none',
                padding: '16px 14px',
                fontSize: 22,
                fontWeight: 600,
                color: 'var(--text-1)',
                fontFamily: 'var(--font-mono)',
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
                padding: '9px 12px',
                borderRadius: 10,
                marginRight: 10,
                fontSize: 13,
                colorScheme: 'dark',
              }}
            />
            <button
              className="es-btn primary"
              style={{ height: 44, padding: '0 22px', fontSize: 14, marginRight: 6 }}
              onClick={onStart}
              disabled={submitting || helperBlocked || form.analysts.length === 0}
            >
              {submitting ? 'Starting…' : 'Start analysis →'}
            </button>
          </div>
        </div>

        {/* disclosure */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            margin: '16px 4px 8px',
            fontSize: 12,
            color: 'var(--text-3)',
          }}
        >
          <button
            className="chip active"
            style={{
              background: 'var(--accent-bg)',
              color: 'var(--accent-hi)',
            }}
            onClick={onClose}
          >
            − Hide advanced
          </button>
          <span>Editing settings · changes apply at start</span>
          <div style={{ flex: 1 }} />
        </div>

        {/* advanced panel */}
        <div className="es-card">
          {/* Depth */}
          <div className="adv-row">
            <div className="adv-label">
              <div className="adv-title">Depth</div>
              <div className="adv-sub">
                How many debate rounds between bull/bear researchers.
              </div>
            </div>
            <div className="adv-control">
              <div className="depth-grid">
                {DEPTHS.map((d) => (
                  <label
                    key={d.value}
                    className={`depth-card ${form.depth === d.value ? 'active' : ''}`}
                  >
                    <input
                      type="radio"
                      name="depth"
                      checked={form.depth === d.value}
                      onChange={() => patch({ depth: d.value })}
                    />
                    <div className="depth-title">{d.title}</div>
                    <div className="depth-meta">{d.rounds}</div>
                    <div className="depth-stats">
                      <span>{d.time}</span>
                      <span style={{ color: 'var(--text-4)' }}>·</span>
                      <span>{d.cost}</span>
                    </div>
                  </label>
                ))}
              </div>
            </div>
          </div>

          {/* Analysts */}
          <div className="adv-row">
            <div className="adv-label">
              <div className="adv-title">Analyst team</div>
              <div className="adv-sub">
                Stage 1 of 5. Each analyst contributes a perspective to the debate.
              </div>
            </div>
            <div className="adv-control">
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: '1fr 1fr',
                  gap: 8,
                }}
              >
                {ANALYSTS.map((a) => {
                  const checked = form.analysts.includes(a.id)
                  return (
                    <label
                      key={a.id}
                      className={`check-card ${checked ? 'active' : ''}`}
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleAnalyst(a.id)}
                      />
                      <div>
                        <div style={{ fontSize: 12.5, fontWeight: 500 }}>
                          {a.name}
                        </div>
                        <div
                          style={{
                            fontSize: 11,
                            color: 'var(--text-3)',
                            marginTop: 2,
                          }}
                        >
                          {a.desc}
                        </div>
                      </div>
                    </label>
                  )
                })}
              </div>
            </div>
          </div>

          {/* Model stack */}
          <div className="adv-row">
            <div className="adv-label">
              <div className="adv-title">Model stack</div>
              <div className="adv-sub">
                Provider + the two thinker models. Effort knobs appear per
                provider.
              </div>
            </div>
            <div className="adv-control" style={{ display: 'grid', gap: 12 }}>
              <div className="seg" style={{ width: '100%' }}>
                {(config?.providers ?? []).map((p) => (
                  <button
                    key={p.key}
                    className={`seg-btn ${form.provider === p.key ? 'active' : ''}`}
                    style={{ flex: 1 }}
                    onClick={() => {
                      const ms = config?.models_by_provider[p.key] ?? []
                      patch({
                        provider: p.key,
                        backendUrl: p.backend_url ?? undefined,
                        shallowThinker: ms[0]?.id ?? '',
                        deepThinker: ms[1]?.id ?? ms[0]?.id ?? '',
                      })
                    }}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
              {provider?.requires_helper && (
                <HelperSetup status={helperStatus} />
              )}
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: '1fr 1fr',
                  gap: 12,
                }}
              >
                <Field label="Shallow thinker (quick)">
                  <select
                    className="fld-input"
                    value={form.shallowThinker}
                    onChange={(e) => patch({ shallowThinker: e.target.value })}
                  >
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Deep thinker (slow, smarter)">
                  <select
                    className="fld-input"
                    value={form.deepThinker}
                    onChange={(e) => patch({ deepThinker: e.target.value })}
                  >
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.label}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              {supportsReasoning && (
                <Field label="Reasoning effort">
                  <div className="seg" style={{ width: '100%' }}>
                    {REASONING.map((r) => (
                      <button
                        key={r}
                        className={`seg-btn ${form.reasoningEffort === r || (!form.reasoningEffort && r === 'auto') ? 'active' : ''}`}
                        style={{ flex: 1 }}
                        onClick={() => patch({ reasoningEffort: r })}
                      >
                        {r}
                      </button>
                    ))}
                  </div>
                </Field>
              )}
              <Field label="Backend URL" hint="Self-hosted endpoint override.">
                <input
                  className="fld-input"
                  placeholder={provider?.backend_url ?? 'https://api.openai.com/v1'}
                  value={form.backendUrl ?? ''}
                  onChange={(e) =>
                    patch({ backendUrl: e.target.value || undefined })
                  }
                />
              </Field>
            </div>
          </div>

          {/* Output */}
          <div className="adv-row" style={{ borderBottom: 'none' }}>
            <div className="adv-label">
              <div className="adv-title">Output</div>
              <div className="adv-sub">Final report language and crash-safety.</div>
            </div>
            <div className="adv-control" style={{ display: 'grid', gap: 12 }}>
              <Field label="Language">
                <select
                  className="fld-input"
                  value={form.language}
                  onChange={(e) => patch({ language: e.target.value })}
                >
                  {(config?.output_languages ?? [
                    { value: 'English', label: 'English (default)' },
                  ]).map((l) => (
                    <option key={l.value} value={l.value}>
                      {l.label}
                    </option>
                  ))}
                </select>
              </Field>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                }}
              >
                <label className="switch">
                  <input
                    type="checkbox"
                    checked={form.checkpoint}
                    onChange={(e) =>
                      patch({ checkpoint: e.target.checked })
                    }
                  />
                  <span />
                </label>
                <div>
                  <div style={{ fontSize: 12.5, fontWeight: 500 }}>
                    Checkpoint resume
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                    Recover from a crash or refresh mid-run.
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* sticky footer */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            marginTop: 20,
            padding: '14px 18px',
            background: 'var(--bg-1)',
            border: '1px solid var(--line-1)',
            borderRadius: 12,
          }}
        >
          <div style={{ fontSize: 12, color: 'var(--text-3)' }}>
            Run estimate:{' '}
            <span style={{ color: 'var(--text-1)', fontWeight: 500 }}>
              {DEPTHS.find((d) => d.value === form.depth)?.time ?? '—'}
            </span>
            <span style={{ margin: '0 8px', color: 'var(--text-4)' }}>·</span>
            <span style={{ color: 'var(--text-1)', fontWeight: 500 }}>
              {DEPTHS.find((d) => d.value === form.depth)?.cost ?? '—'}
            </span>
          </div>
          <div style={{ flex: 1 }} />
          <button className="es-btn" onClick={onClose}>
            Back
          </button>
          <button
            className="es-btn primary"
            style={{ height: 38, padding: '0 22px' }}
            onClick={onStart}
            disabled={submitting || helperBlocked || form.analysts.length === 0}
          >
            {submitting ? 'Starting…' : 'Start analysis →'}
          </button>
        </div>
      </div>
    </div>
  )
}

function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <label
        style={{
          fontSize: 11.5,
          fontWeight: 500,
          color: 'var(--text-2)',
        }}
      >
        {label}
      </label>
      {children}
      {hint && (
        <div style={{ fontSize: 11.5, color: 'var(--text-3)' }}>{hint}</div>
      )}
    </div>
  )
}
