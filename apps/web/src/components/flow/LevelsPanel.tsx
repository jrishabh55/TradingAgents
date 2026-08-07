import { useEffect, useState } from 'react'
import { api } from '#/lib/api'
import type { LevelValue, LevelsResponse } from '#/lib/types'

/* Debounce so dragging through capital digits doesn't fire a request per
   keystroke. The endpoint is cheap, but 8 in-flight requests would still race
   each other to set state. */
const DEBOUNCE_MS = 400

const DEFAULT_CAPITAL = 1_000_000
const DEFAULT_RISK_PCT = 1
const DEFAULT_R = 2

function fmt(value: number, currency?: string | null): string {
  const n = value.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  return currency ? `${n} ${currency}` : n
}

/** One row of the price ladder. `basis` is the whole point — never hide it. */
function LevelRow({
  label,
  value,
  currency,
  tone,
}: {
  label: string
  value: LevelValue
  currency?: string | null
  tone?: 'ok' | 'err' | 'accent'
}) {
  const color =
    tone === 'ok'
      ? 'var(--ok)'
      : tone === 'err'
        ? 'var(--err)'
        : tone === 'accent'
          ? 'var(--accent-hi)'
          : 'var(--text-1)'
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '110px 1fr',
        gap: 12,
        padding: '10px 0',
        borderBottom: '1px solid var(--line-1)',
        alignItems: 'baseline',
      }}
    >
      <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{label}</div>
      <div>
        <div
          className="es-mono"
          style={{ fontSize: 15, color, fontWeight: 500 }}
        >
          {fmt(value.price, currency)}
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-4)', marginTop: 3 }}>
          {value.basis}
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div
        style={{
          fontSize: 10.5,
          letterSpacing: '0.08em',
          textTransform: 'uppercase',
          color: 'var(--text-4)',
        }}
      >
        {label}
      </div>
      <div
        className="es-mono"
        style={{ fontSize: 14, color: 'var(--text-1)', marginTop: 4 }}
      >
        {value}
      </div>
    </div>
  )
}

export function LevelsPanel({ runId }: { runId: string }) {
  const [capital, setCapital] = useState(String(DEFAULT_CAPITAL))
  const [riskPct, setRiskPct] = useState(String(DEFAULT_RISK_PCT))
  const [rMultiple, setRMultiple] = useState(String(DEFAULT_R))
  const [data, setData] = useState<LevelsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const capitalNum = Number(capital)
    const riskNum = Number(riskPct)
    const rNum = Number(rMultiple)
    /* Don't fire on half-typed input — the server would 422 and the user would
       see an error for a value they're still typing. */
    if (!(capitalNum > 0) || !(riskNum > 0) || !(rNum >= 1)) return

    let cancelled = false
    setLoading(true)
    const timer = setTimeout(() => {
      api
        .runLevels(runId, {
          capital: capitalNum,
          risk_pct: riskNum,
          r_multiple: rNum,
        })
        .then((res) => {
          if (cancelled) return
          setData(res)
          setError(null)
        })
        .catch((e: unknown) => {
          if (cancelled) return
          setError(e instanceof Error ? e.message : String(e))
        })
        .finally(() => {
          if (!cancelled) setLoading(false)
        })
    }, DEBOUNCE_MS)

    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [runId, capital, riskPct, rMultiple])

  const levels = data?.levels
  const size = data?.size
  const currency = data?.currency

  return (
    <div style={{ padding: '20px 28px 28px' }}>
      {/* Inputs — sizing recomputes as these change, no pipeline re-run. */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(3, 1fr)',
          gap: 12,
          marginBottom: 18,
        }}
      >
        <label style={{ display: 'block' }}>
          <span
            style={{
              fontSize: 11,
              color: 'var(--text-3)',
              display: 'block',
              marginBottom: 5,
            }}
          >
            Capital{currency ? ` (${currency})` : ''}
          </span>
          <input
            className="fld-input es-mono"
            type="number"
            min={1}
            step={1000}
            value={capital}
            onChange={(e) => setCapital(e.target.value)}
          />
        </label>
        <label style={{ display: 'block' }}>
          <span
            style={{
              fontSize: 11,
              color: 'var(--text-3)',
              display: 'block',
              marginBottom: 5,
            }}
          >
            Risk per trade (%)
          </span>
          <input
            className="fld-input es-mono"
            type="number"
            min={0.1}
            max={100}
            step={0.5}
            value={riskPct}
            onChange={(e) => setRiskPct(e.target.value)}
          />
        </label>
        <label style={{ display: 'block' }}>
          <span
            style={{
              fontSize: 11,
              color: 'var(--text-3)',
              display: 'block',
              marginBottom: 5,
            }}
          >
            Target (R multiple)
          </span>
          <input
            className="fld-input es-mono"
            type="number"
            min={1}
            max={10}
            step={0.5}
            value={rMultiple}
            onChange={(e) => setRMultiple(e.target.value)}
          />
        </label>
      </div>

      {currency && (
        <div
          style={{ fontSize: 11, color: 'var(--text-4)', marginBottom: 16 }}
        >
          Capital is read in {currency} — the instrument's quote currency. No FX
          conversion is applied.
        </div>
      )}

      {error && (
        <div
          className="es-card"
          style={{
            padding: 16,
            color: 'var(--err)',
            fontSize: 12.5,
            background: 'var(--err-bg)',
          }}
        >
          {error}
        </div>
      )}

      {!error && !data && loading && (
        <div style={{ fontSize: 12.5, color: 'var(--text-3)' }}>
          Computing levels…
        </div>
      )}

      {data && (
        <div style={{ opacity: loading ? 0.55 : 1, transition: 'opacity .15s' }}>
          {/* Verdict */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              flexWrap: 'wrap',
              marginBottom: 14,
            }}
          >
            <span className={`es-pill ${data.viable ? 'ok' : 'err'}`}>
              <span className="es-dot" />
              {data.viable ? 'Setup meets the rules' : 'Rules say skip'}
            </span>
            {data.rating && <span className="es-pill accent">{data.rating}</span>}
            {levels && (
              <span className="es-pill">
                R:R {levels.reward_risk_ratio.toFixed(2)}:1
              </span>
            )}
          </div>

          {data.viability_notes.length > 0 && (
            <ul
              style={{
                margin: '0 0 18px',
                padding: '12px 14px 12px 30px',
                background: 'var(--bg-2)',
                border: '1px solid var(--line-1)',
                borderRadius: 10,
                fontSize: 12.5,
                color: 'var(--text-2)',
                lineHeight: 1.55,
              }}
            >
              {data.viability_notes.map((note) => (
                <li key={note} style={{ marginBottom: 4 }}>
                  {note}
                </li>
              ))}
            </ul>
          )}

          {levels && (
            <>
              <LevelRow
                label="Target"
                value={levels.target}
                currency={currency}
                tone="ok"
              />
              <LevelRow
                label={`Target (+1R)`}
                value={levels.target_alt}
                currency={currency}
              />
              {levels.resistance && (
                <LevelRow
                  label="Resistance"
                  value={levels.resistance}
                  currency={currency}
                  tone="accent"
                />
              )}
              <LevelRow
                label="Entry"
                value={levels.entry}
                currency={currency}
              />
              <LevelRow
                label="Stop"
                value={levels.stop}
                currency={currency}
                tone="err"
              />

              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
                  gap: 16,
                  padding: '18px 0',
                  borderBottom: '1px solid var(--line-1)',
                }}
              >
                <Stat
                  label="Risk / share"
                  value={fmt(levels.risk_per_share, currency)}
                />
                <Stat
                  label="Stop distance"
                  value={`${levels.risk_pct_of_entry.toFixed(2)}%`}
                />
                <Stat
                  label="Reward : risk"
                  value={`${levels.reward_risk_ratio.toFixed(2)} : 1`}
                />
              </div>
            </>
          )}

          {size && (
            <div style={{ padding: '18px 0' }}>
              <div
                style={{
                  fontSize: 10.5,
                  letterSpacing: '0.08em',
                  textTransform: 'uppercase',
                  color: 'var(--text-4)',
                  marginBottom: 12,
                }}
              >
                Position size
              </div>
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
                  gap: 16,
                }}
              >
                <Stat label="Shares" value={size.shares.toLocaleString()} />
                <Stat
                  label="Cash at risk"
                  value={fmt(size.cash_risk, currency)}
                />
                <Stat
                  label="Position value"
                  value={fmt(size.position_value, currency)}
                />
              </div>
              <div
                style={{
                  fontSize: 11,
                  color: 'var(--text-4)',
                  marginTop: 10,
                }}
              >
                {size.basis}
              </div>
            </div>
          )}

          {/* What the agent claimed, side by side with what was computed. */}
          {data.divergence && (
            <div
              style={{
                marginTop: 6,
                padding: '12px 14px',
                background: 'var(--bg-2)',
                border: '1px solid var(--line-1)',
                borderLeft: '2px solid var(--run)',
                borderRadius: 8,
                fontSize: 12,
                color: 'var(--text-2)',
                lineHeight: 1.55,
              }}
            >
              <strong style={{ color: 'var(--text-1)' }}>Agent vs computed</strong>
              <div style={{ marginTop: 4 }}>{data.divergence}</div>
              {data.model_suggested?.position_sizing && (
                <div style={{ marginTop: 4, color: 'var(--text-3)' }}>
                  Agent sizing: {data.model_suggested.position_sizing}
                </div>
              )}
            </div>
          )}

          <div
            style={{
              marginTop: 18,
              fontSize: 11,
              color: 'var(--text-4)',
              lineHeight: 1.6,
            }}
          >
            {data.disclaimer}
          </div>
        </div>
      )}
    </div>
  )
}
