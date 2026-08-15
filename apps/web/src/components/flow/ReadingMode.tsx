import { useState } from 'react'
import { TEAMS, totalAgents } from '#/lib/teams'
import { Topbar } from '#/components/shared/Topbar'
import { StatusBar } from '#/components/shared/StatusBar'
import { ReportBody } from '#/components/shared/ReportBody'
import { LevelsPanel } from '#/components/flow/LevelsPanel'
import { api } from '#/lib/api'
import { ratingTooltip } from '#/lib/rating'
import type { RunDetail } from '#/lib/types'

const TABS = [
  { id: 'decision', label: 'Decision' },
  /* Computed stop/target/size — not part of the agents' output, so it renders
     its own panel rather than markdown. */
  { id: 'levels', label: 'Levels' },
  { id: 'bull', label: 'Bull case' },
  { id: 'bear', label: 'Bear case' },
  { id: 'risk', label: 'Risk' },
  { id: 'plan', label: 'Trade plan' },
  { id: 'transcript', label: 'Transcript ›' },
] as const

type TabId = (typeof TABS)[number]['id']

function elapsedFor(run: RunDetail): string {
  if (!run.started_at || !run.finished_at) return '—'
  const ms =
    Date.parse(run.finished_at) - Date.parse(run.started_at)
  if (Number.isNaN(ms) || ms < 0) return '—'
  const total = Math.floor(ms / 1000)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${s.toString().padStart(2, '0')}`
}

function pickByTab(run: RunDetail, tab: TabId): string | null | undefined {
  switch (tab) {
    case 'levels':
      return null /* rendered by LevelsPanel, not markdown */
    case 'decision':
      return run.decision_text || run.final_trade_decision
    case 'bull':
      return (run.investment_debate_state as { bull_history?: string } | null)
        ?.bull_history
    case 'bear':
      return (run.investment_debate_state as { bear_history?: string } | null)
        ?.bear_history
    case 'risk':
      return (run.risk_debate_state as { history?: string } | null)?.history
    case 'plan':
      return run.trader_investment_plan || run.investment_plan
    case 'transcript':
      return [
        run.market_report && `## Market\n${run.market_report}`,
        run.sentiment_report && `## Social\n${run.sentiment_report}`,
        run.news_report && `## News\n${run.news_report}`,
        run.fundamentals_report &&
          `## Fundamentals\n${run.fundamentals_report}`,
        run.investment_plan &&
          `## Research debate verdict\n${run.investment_plan}`,
        run.trader_investment_plan &&
          `## Trader plan\n${run.trader_investment_plan}`,
        run.final_trade_decision &&
          `## Portfolio decision\n${run.final_trade_decision}`,
      ]
        .filter(Boolean)
        .join('\n\n')
  }
}

export function ReadingMode({ run }: { run: RunDetail }) {
  const [tab, setTab] = useState<TabId>('decision')
  const failed = run.status === 'failed' || run.status === 'cancelled'

  return (
    <div className="es-art">
      <Topbar
        ticker={run.ticker}
        date={run.analysis_date}
        state={failed ? 'failed' : 'done'}
        total={elapsedFor(run)}
        onExport={() =>
          window.open(api.reportUrl(run.id), '_blank', 'noopener')
        }
      />

      <div
        style={{
          flex: 1,
          display: 'grid',
          gridTemplateColumns: '240px 1fr',
          minHeight: 0,
        }}
      >
        {/* Agent rail */}
        <div
          style={{
            borderRight: '1px solid var(--line-1)',
            background: 'var(--bg-1)',
            display: 'flex',
            flexDirection: 'column',
            padding: '14px 0',
            overflow: 'auto',
          }}
        >
          <div
            style={{
              padding: '0 16px 10px',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            <span className="es-team-label" style={{ flex: 1 }}>
              Agents · {totalAgents}
            </span>
          </div>
          {TEAMS.map((team) => (
            <div key={team.id} style={{ marginBottom: 8 }}>
              <div
                style={{
                  padding: '6px 16px',
                  fontSize: 10.5,
                  fontWeight: 600,
                  letterSpacing: '0.08em',
                  textTransform: 'uppercase',
                  color: 'var(--text-4)',
                }}
              >
                {team.name}
              </div>
              {team.agents.map((a) => (
                <div
                  key={a.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '7px 16px',
                    fontSize: 12.5,
                    color: 'var(--text-2)',
                    cursor: 'default',
                  }}
                >
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: 50,
                      background: failed ? 'var(--text-4)' : 'var(--ok)',
                    }}
                  />
                  <span
                    style={{
                      flex: 1,
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {a.name}
                  </span>
                </div>
              ))}
            </div>
          ))}
        </div>

        {/* Reading area */}
        <div
          style={{
            overflow: 'auto',
            display: 'flex',
            justifyContent: 'center',
            padding: '24px 24px 60px',
          }}
        >
          <div style={{ width: '100%', maxWidth: 760, position: 'relative' }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                marginBottom: 18,
                flexWrap: 'wrap',
              }}
            >
              <span
                className={`es-pill ${failed ? 'err' : 'ok'}`}
              >
                <span className="es-dot" />
                {failed
                  ? `Run ${run.status}`
                  : `Run complete · ${elapsedFor(run)}`}
              </span>
              <span className="es-pill">{run.ticker}</span>
              <span className="es-pill">{run.analysis_date}</span>
              {run.rating && (
                <span className="es-pill accent" title={ratingTooltip(run.rating)}>
                  {run.rating}
                </span>
              )}
              <div style={{ flex: 1 }} />
              <a
                className="es-btn sm no-underline"
                href={api.reportUrl(run.id)}
                target="_blank"
                rel="noreferrer"
              >
                Export ▾
              </a>
            </div>

            <div
              className="es-card"
              style={{ background: 'var(--bg-1)', borderRadius: 14 }}
            >
              <div
                style={{
                  padding: '8px 28px',
                  borderBottom: '1px solid var(--line-1)',
                  display: 'flex',
                  gap: 16,
                  fontSize: 12,
                }}
              >
                {TABS.map((t) => {
                  const active = tab === t.id
                  return (
                    <button
                      key={t.id}
                      onClick={() => setTab(t.id)}
                      style={{
                        background: 'transparent',
                        border: 0,
                        cursor: 'pointer',
                        color: active ? 'var(--text-1)' : 'var(--text-3)',
                        padding: '10px 0',
                        borderBottom: active
                          ? '2px solid var(--accent)'
                          : '2px solid transparent',
                        fontFamily: 'inherit',
                        fontSize: 12,
                      }}
                    >
                      {t.label}
                    </button>
                  )
                })}
              </div>
              {failed ? (
                <div
                  style={{
                    padding: 24,
                    color: 'var(--err)',
                    fontSize: 13,
                  }}
                >
                  {run.error ?? 'Run did not complete.'}
                </div>
              ) : tab === 'levels' ? (
                <LevelsPanel runId={run.id} />
              ) : (
                <ReportBody
                  markdown={pickByTab(run, tab) ?? ''}
                  ticker={tab === 'decision' ? run.ticker : undefined}
                  rating={tab === 'decision' ? run.rating : undefined}
                />
              )}
            </div>
          </div>
        </div>
      </div>

      <StatusBar
        toolCalls={0}
        llmCalls={0}
        reports={
          [
            run.market_report,
            run.sentiment_report,
            run.news_report,
            run.fundamentals_report,
            run.investment_plan,
            run.trader_investment_plan,
            run.final_trade_decision,
          ].filter(Boolean).length
        }
        tokens="—"
        cost="—"
        rightLabel={
          run.finished_at
            ? `saved ${new Date(run.finished_at).toLocaleString()}`
            : 'saved'
        }
      />
    </div>
  )
}
