import { useEffect, useMemo, useRef, useState } from 'react'
import { TEAMS, totalAgents } from '#/lib/teams'
import type { AgentStatus } from '#/lib/teams'
import { useRunEvents } from '#/lib/sse'
import { api } from '#/lib/api'
import type { RunDetail, SseEvent } from '#/lib/types'
import { Topbar } from '#/components/shared/Topbar'
import { StatusBar } from '#/components/shared/StatusBar'
import { ReportBody } from '#/components/shared/ReportBody'

const ANALYST_TO_REPORT: Record<string, keyof RunDetail> = {
  market: 'market_report',
  social: 'sentiment_report',
  news: 'news_report',
  fundamentals: 'fundamentals_report',
}

const SECTION_TO_REPORT: Record<string, keyof RunDetail> = {
  investment_plan: 'investment_plan',
  trader_investment_plan: 'trader_investment_plan',
  final_trade_decision: 'final_trade_decision',
}

/* Map: report key → which agent id is responsible. The agent's status
   determines whether the report's tab shows a "live" pip. Pipeline order
   doubles as tab order. */
const REPORT_TABS: {
  key: keyof RunDetail
  label: string
  agentId: string
}[] = [
  { key: 'market_report', label: 'Market', agentId: 'market' },
  { key: 'sentiment_report', label: 'Social', agentId: 'social' },
  { key: 'news_report', label: 'News', agentId: 'news' },
  { key: 'fundamentals_report', label: 'Fundamentals', agentId: 'fundamentals' },
  { key: 'investment_plan', label: 'Research', agentId: 'research_manager' },
  { key: 'trader_investment_plan', label: 'Trader', agentId: 'trader' },
  { key: 'final_trade_decision', label: 'Decision', agentId: 'portfolio' },
]

interface AgentState {
  status: AgentStatus
  startedAt?: number // ms epoch — set when status first becomes 'running'
  finishedAt?: number // ms epoch — set when transitioning to 'done'/'error'
}

interface DerivedState {
  agents: Record<string, AgentState>
  reports: Partial<Record<keyof RunDetail, string>>
  activity: ActivityRow[]
  toolCalls: number
  debateUpdates: number
  finalRating?: string
  finalText?: string
  failed?: string
}

/* Flatten TEAMS into the canonical pipeline execution order. Walking this
   order is how we derive "current" / "next" / "lastDone" from agent
   statuses without relying on event-side-effects (webapp1 doesn't replay
   events on first SSE connect, so side-effect tracking misses pre-existing
   completions from the polled RunDetail). */
const PIPELINE_ORDER: string[] = (() => {
  const ids: string[] = []
  for (const team of TEAMS) for (const a of team.agents) ids.push(a.id)
  return ids
})()

interface PipelineProgression {
  /* Agent id whose status is `running` (real, event-confirmed). */
  activeId: string | null
  /* First queued agent in pipeline order, used as the "preparing…" guess
     when no agent is event-confirmed running but the run is still going. */
  preparingId: string | null
  /* Last (in pipeline order) agent whose status is `done`. */
  lastDoneId: string | null
}

function deriveProgression(
  agents: Record<string, AgentState>,
): PipelineProgression {
  let active: string | null = null
  let lastDone: string | null = null
  let preparing: string | null = null
  for (const id of PIPELINE_ORDER) {
    const s = agents[id]?.status
    if (s === 'running' && !active) active = id
    if (s === 'done') lastDone = id
    if (s === 'queued' && !preparing) preparing = id
  }
  return { activeId: active, preparingId: preparing, lastDoneId: lastDone }
}

interface ActivityRow {
  seq: number
  time: string
  agent: string
  type: 'Tool' | 'Decision' | 'Reasoning' | 'Section' | 'Status'
  body: string
}

/* Helper: transition an agent to a new status, stamping started/finished at
   the moment of the event (or the wall clock if the event lacks `ts`).
   We don't track currentAgentId / lastFinishedId here — those are derived
   from the agent state map at render time via deriveProgression(). */
function setStatus(
  state: DerivedState,
  id: string,
  next: AgentStatus,
  at: number,
) {
  const prev = state.agents[id]
  if (!prev) return
  /* Don't downgrade: if an agent is 'done', a later 'running' from a stray
     event shouldn't reset it. */
  if (prev.status === 'done' && next === 'running') return

  const updated: AgentState = { ...prev, status: next }
  if (next === 'running' && prev.status !== 'running') {
    updated.startedAt = at
  }
  if ((next === 'done' || next === 'error') && prev.status !== next) {
    updated.finishedAt = at
  }
  state.agents[id] = updated
}

function tsToMs(ts: unknown, fallback: number): number {
  if (typeof ts === 'string') {
    const t = Date.parse(ts)
    if (!Number.isNaN(t)) return t
  }
  if (typeof ts === 'number') return ts
  return fallback
}

function deriveState(initial: RunDetail, events: SseEvent[]): DerivedState {
  const state: DerivedState = {
    agents: {},
    reports: {
      market_report: initial.market_report ?? undefined,
      sentiment_report: initial.sentiment_report ?? undefined,
      news_report: initial.news_report ?? undefined,
      fundamentals_report: initial.fundamentals_report ?? undefined,
      investment_plan: initial.investment_plan ?? undefined,
      trader_investment_plan: initial.trader_investment_plan ?? undefined,
      final_trade_decision: initial.final_trade_decision ?? undefined,
    },
    activity: [],
    toolCalls: 0,
    debateUpdates: 0,
    finalRating: initial.rating ?? undefined,
    finalText: initial.decision_text ?? undefined,
  }
  for (const team of TEAMS) {
    for (const agent of team.agents) {
      state.agents[agent.id] = { status: 'queued' }
    }
  }
  for (const [analyst, key] of Object.entries(ANALYST_TO_REPORT)) {
    if (initial[key]) state.agents[analyst] = { status: 'done' }
  }

  for (const ev of events) {
    const d = ev.data as Record<string, unknown>
    const evMs = tsToMs(d.ts, Date.now())
    const tsLabel = new Date(evMs).toISOString().slice(11, 19)
    switch (ev.type) {
      case 'analyst.started': {
        const a = String(d.analyst ?? '')
        setStatus(state, a, 'running', evMs)
        state.activity.push({
          seq: ev.seq,
          time: tsLabel,
          agent: a,
          type: 'Status',
          body: `${a} started`,
        })
        break
      }
      case 'analyst.report': {
        const a = String(d.analyst ?? '')
        const section = String(d.section ?? '')
        const content = String(d.content ?? '')
        if (a) setStatus(state, a, 'running', evMs)
        const key = ANALYST_TO_REPORT[a] ?? (section as keyof RunDetail)
        if (key) {
          const prev = state.reports[key]
          if (!prev || content.length >= prev.length) {
            state.reports[key] = content
          }
        }
        break
      }
      case 'analyst.completed': {
        const a = String(d.analyst ?? '')
        setStatus(state, a, 'done', evMs)
        state.activity.push({
          seq: ev.seq,
          time: tsLabel,
          agent: a,
          type: 'Status',
          body: `${a} completed`,
        })
        break
      }
      case 'team.started': {
        const team = String(d.team ?? '')
        const teamNode = TEAMS.find((t) => t.id === team)
        if (teamNode) {
          /* Promote the first queued agent in the team to running so the
             user sees something happening immediately. The graph will emit
             agent-specific events shortly after. */
          const first = teamNode.agents.find(
            (a) => state.agents[a.id]?.status === 'queued',
          )
          if (first) setStatus(state, first.id, 'running', evMs)
        }
        break
      }
      case 'team.completed': {
        const team = String(d.team ?? '')
        const teamNode = TEAMS.find((t) => t.id === team)
        if (teamNode) {
          for (const a of teamNode.agents) {
            setStatus(state, a.id, 'done', evMs)
          }
        }
        break
      }
      case 'debate.update': {
        state.debateUpdates += 1
        const role = String(d.role ?? '')
        const team = String(d.team ?? '')
        const delta = String(d.delta ?? d.full ?? '')
        state.activity.push({
          seq: ev.seq,
          time: tsLabel,
          agent: role || team,
          type: 'Reasoning',
          body: delta.slice(0, 500),
        })
        const roleToAgent: Record<string, string> = {
          bull: 'bull',
          bear: 'bear',
          research_manager: 'research_manager',
          risky: 'risky',
          neutral: 'neutral',
          safe: 'safe',
        }
        const agentId = roleToAgent[role]
        if (agentId) setStatus(state, agentId, 'running', evMs)
        break
      }
      case 'report.section': {
        const section = String(d.section ?? '')
        const content = String(d.content ?? '')
        const key = SECTION_TO_REPORT[section]
        if (key) {
          const prev = state.reports[key]
          if (!prev || content.length >= prev.length) {
            state.reports[key] = content
          }
        }
        state.activity.push({
          seq: ev.seq,
          time: tsLabel,
          agent: section,
          type: 'Section',
          body: `${section} updated`,
        })
        if (section === 'investment_plan')
          setStatus(state, 'research_manager', 'done', evMs)
        if (section === 'trader_investment_plan')
          setStatus(state, 'trader', 'done', evMs)
        if (section === 'final_trade_decision')
          setStatus(state, 'portfolio', 'done', evMs)
        break
      }
      case 'tool.called': {
        state.toolCalls += 1
        state.activity.push({
          seq: ev.seq,
          time: tsLabel,
          agent: String(d.agent ?? 'tool'),
          type: 'Tool',
          body: `${d.name ?? 'tool'}(${stringifyArgs(d.args)})`,
        })
        break
      }
      case 'run.final': {
        state.finalRating = (d.rating as string) ?? state.finalRating
        state.finalText = (d.decision_text as string) ?? state.finalText
        for (const id of Object.keys(state.agents)) {
          if (state.agents[id].status === 'running')
            setStatus(state, id, 'done', evMs)
        }
        break
      }
      case 'run.failed': {
        state.failed = String(d.error ?? 'run failed')
        for (const id of Object.keys(state.agents)) {
          if (state.agents[id].status === 'running')
            setStatus(state, id, 'error', evMs)
        }
        break
      }
      case 'run.cancelled': {
        state.failed = 'cancelled'
        break
      }
      default:
        break
    }
  }
  return state
}

function stringifyArgs(args: unknown): string {
  if (!args) return ''
  if (typeof args === 'string') return args
  try {
    return Object.entries(args as Record<string, unknown>)
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join(', ')
  } catch {
    return ''
  }
}

function fmtElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m}:${s.toString().padStart(2, '0')}`
}

export function AgentViewLive({ run }: { run: RunDetail }) {
  const sse = useRunEvents(run.id)
  const [now, setNow] = useState(Date.now())
  const startedAtMs = useMemo(() => {
    if (run.started_at) return Date.parse(run.started_at)
    return Date.parse(run.created_at)
  }, [run.started_at, run.created_at])

  useEffect(() => {
    if (run.status !== 'running' && run.status !== 'queued') return
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [run.status])

  const elapsed = fmtElapsed(Math.floor(Math.max(0, now - startedAtMs) / 1000))
  const derived = useMemo(() => deriveState(run, sse.events), [run, sse.events])

  /* Available report tabs: any tab whose key has content. Order is the
     pipeline order; the auto-active tab is the latest available unless the
     user has pinned one. We track previously-seen-keys so we can flag
     freshly-arrived tabs with the `.fresh` slide-in animation. */
  const availableTabs = REPORT_TABS.filter((t) => derived.reports[t.key])
  const latestKey = availableTabs[availableTabs.length - 1]?.key ?? null

  const seenKeysRef = useRef<Set<string>>(new Set())
  const freshKey = useMemo(() => {
    let f: keyof RunDetail | null = null
    for (const t of availableTabs) {
      if (!seenKeysRef.current.has(t.key)) {
        f = t.key
        seenKeysRef.current.add(t.key)
      }
    }
    return f
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availableTabs.length])

  const [pinnedKey, setPinnedKey] = useState<keyof RunDetail | null>(null)
  /* If the user has pinned a tab that's still in the available list, honour
     it. Otherwise auto-follow the latest. */
  const activeKey =
    pinnedKey && availableTabs.some((t) => t.key === pinnedKey)
      ? pinnedKey
      : latestKey

  const completedReports = availableTabs.length
  const liveCount = Object.values(derived.agents).filter(
    (a) => a.status === 'running',
  ).length

  const activeContent = activeKey ? derived.reports[activeKey] : undefined
  const activeTab = REPORT_TABS.find((t) => t.key === activeKey)
  const activeIsLive =
    !!activeTab && derived.agents[activeTab.agentId]?.status === 'running'

  /* Lookup helpers for the Now-running banner. */
  const agentLookup = useMemo(() => {
    const m: Record<string, { name: string; team: string }> = {}
    for (const team of TEAMS)
      for (const a of team.agents) m[a.id] = { name: a.name, team: team.name }
    return m
  }, [])

  /* Derive progression at render time from the agent state map — robust to
     webapp1 not replaying events on first SSE connect. */
  const progression = useMemo(
    () => deriveProgression(derived.agents),
    [derived.agents],
  )

  const isRunActive = run.status === 'running' || run.status === 'queued'
  /* If a real running agent is known, that's the active one. Otherwise, when
     the run is still progressing, fall back to the next-queued agent and
     mark it as "preparing" (we don't have an SSE confirmation yet but the
     pipeline order tells us this is up next). */
  const activeId = progression.activeId
  const preparingId =
    !activeId && isRunActive ? progression.preparingId : null
  const focalId = activeId ?? preparingId
  const focalAgent = focalId ? agentLookup[focalId] : null
  const lastDoneAgent = progression.lastDoneId
    ? agentLookup[progression.lastDoneId]
    : null
  const focalStartedAt = activeId
    ? derived.agents[activeId]?.startedAt
    : undefined
  const focalElapsed = focalStartedAt
    ? fmtElapsed(Math.max(0, Math.floor((now - focalStartedAt) / 1000)))
    : null

  return (
    <div className="es-art">
      <Topbar
        ticker={run.ticker}
        date={run.analysis_date}
        state="running"
        elapsed={elapsed}
        onCancel={() => api.cancelRun(run.id).catch(() => {})}
      />

      <div
        style={{
          flex: 1,
          display: 'grid',
          gridTemplateColumns: '260px 1fr 360px',
          gap: 12,
          padding: 12,
          minHeight: 0, // critical: lets children's overflow:auto kick in
        }}
      >
        {/* Pipeline */}
        <div className="es-card">
          <div className="es-panel-head">
            <span>Pipeline</span>
            <div style={{ flex: 1 }} />
            <span
              style={{
                fontSize: 11,
                color: 'var(--text-3)',
                textTransform: 'none',
                letterSpacing: 0,
                fontWeight: 400,
              }}
            >
              {Object.values(derived.agents).filter((a) => a.status === 'done')
                .length}
              /{totalAgents}
            </span>
          </div>
          <div className="es-panel-body" style={{ padding: '10px 6px' }}>
            {TEAMS.map((team) => (
              <div key={team.id} style={{ marginBottom: 14 }}>
                <div
                  style={{
                    padding: '4px 12px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                  }}
                >
                  <span className="es-team-label">{team.name}</span>
                  <div
                    style={{
                      flex: 1,
                      height: 1,
                      background: 'var(--line-1)',
                    }}
                  />
                </div>
                <div>
                  {team.agents.map((a) => {
                    const agentState = derived.agents[a.id] ?? {
                      status: 'queued' as AgentStatus,
                    }
                    const active = agentState.status === 'running'
                    const isDone = agentState.status === 'done'
                    const isPreparing = !active && a.id === preparingId
                    const dotColor = isDone
                      ? 'var(--ok)'
                      : active
                        ? 'var(--run)'
                        : isPreparing
                          ? 'var(--accent)'
                          : agentState.status === 'error'
                            ? 'var(--err)'
                            : 'var(--text-4)'

                    /* Elapsed: live ticker for running, frozen duration for
                       done. Queued / preparing show nothing (we don't know
                       when "preparing" started). */
                    let elapsedLabel: string | null = null
                    if (active && agentState.startedAt) {
                      elapsedLabel = fmtElapsed(
                        Math.max(0, Math.floor((now - agentState.startedAt) / 1000)),
                      )
                    } else if (
                      isDone &&
                      agentState.startedAt &&
                      agentState.finishedAt
                    ) {
                      elapsedLabel = fmtElapsed(
                        Math.max(
                          0,
                          Math.floor(
                            (agentState.finishedAt - agentState.startedAt) / 1000,
                          ),
                        ),
                      )
                    }

                    const rowClass = [
                      'agent-row',
                      active && 'running',
                      isDone && 'done',
                      isPreparing && 'preparing',
                    ]
                      .filter(Boolean)
                      .join(' ')

                    return (
                      <div
                        key={a.id}
                        className={rowClass}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 10,
                          padding: '7px 12px',
                          borderRadius: 6,
                          background: active
                            ? 'var(--accent-bg)'
                            : isPreparing
                              ? 'rgba(79, 124, 255, 0.06)'
                              : 'transparent',
                          margin: '1px 6px',
                          fontSize: 13,
                        }}
                      >
                        <span
                          className={`es-dot ${active || isPreparing ? 'pulse' : ''}`}
                          style={{ color: dotColor, background: dotColor }}
                        />
                        <span
                          className="agent-name"
                          style={{
                            color:
                              agentState.status === 'queued' && !isPreparing
                                ? 'var(--text-4)'
                                : 'var(--text-1)',
                            flex: 1,
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                          }}
                        >
                          {a.name}
                        </span>
                        {isPreparing && (
                          <span
                            style={{
                              fontSize: 10.5,
                              color: 'var(--accent-hi)',
                              fontWeight: 500,
                              fontStyle: 'italic',
                            }}
                          >
                            preparing
                          </span>
                        )}
                        {elapsedLabel && (
                          <span className="agent-elapsed">{elapsedLabel}</span>
                        )}
                        {active && <div className="agent-progress" />}
                      </div>
                    )
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Current report — banner + tabs strip + body */}
        <div className="es-card" style={{ minWidth: 0 }}>
          <div className="es-panel-head">
            <span>Current Report</span>
            <div style={{ flex: 1 }} />
            <span
              style={{
                fontSize: 11,
                color: 'var(--text-3)',
                textTransform: 'none',
                letterSpacing: 0,
                fontWeight: 400,
              }}
            >
              {sse.status === 'open' ? 'connected' : sse.status}
              {liveCount > 0 ? ` · ${liveCount} live` : ''}
            </span>
            {pinnedKey && (
              <button
                className="es-btn ghost sm"
                onClick={() => setPinnedKey(null)}
                title="Auto-follow the newest report"
              >
                Follow latest
              </button>
            )}
            <a
              className="es-btn ghost sm no-underline"
              href={api.reportUrl(run.id)}
              target="_blank"
              rel="noreferrer"
            >
              Markdown
            </a>
          </div>

          {/* "Now running" banner — co-located with the report so the user
             sees agent transitions without looking at the rail. Three modes:
             - active (event-confirmed running): yellow accent, live elapsed
             - preparing (inferred from pipeline order): blue accent, no timer
             - idle (run not in progress): gray */}
          {activeId && focalAgent ? (
            <div
              className="now-running"
              key={`running-${activeId}`}
              role="status"
              aria-live="polite"
            >
              <span className="es-dot pulse" style={{ color: 'var(--run)' }} />
              <span className="arrow">▸</span>
              <span className="name">{focalAgent.name}</span>
              <span className="meta">· {focalAgent.team}</span>
              <div style={{ flex: 1 }} />
              {focalElapsed && (
                <span className="meta es-mono">running {focalElapsed}</span>
              )}
            </div>
          ) : preparingId && focalAgent ? (
            <div
              className="now-running preparing"
              key={`preparing-${preparingId}`}
              role="status"
              aria-live="polite"
            >
              <span
                className="es-dot pulse"
                style={{ color: 'var(--accent)' }}
              />
              <span className="arrow">▸</span>
              <span className="name">{focalAgent.name}</span>
              <span className="meta">
                · {focalAgent.team} · preparing
                {lastDoneAgent ? ` after ${lastDoneAgent.name}` : ''}…
              </span>
            </div>
          ) : lastDoneAgent && !isRunActive ? (
            <div
              className="now-running complete"
              key={`done-${progression.lastDoneId}`}
              role="status"
              aria-live="polite"
            >
              <span className="arrow">✓</span>
              <span className="name">{lastDoneAgent.name}</span>
              <span className="meta">· run complete</span>
            </div>
          ) : (
            <div className="now-running idle" role="status" aria-live="polite">
              <span className="arrow">▸</span>
              <span className="name">Run starting</span>
              <span className="meta">· waiting for the first agent…</span>
            </div>
          )}

          {availableTabs.length > 0 ? (
            <div className="report-tabs" role="tablist">
              {availableTabs.map((t) => {
                const isActive = t.key === activeKey
                const agentRunning =
                  derived.agents[t.agentId]?.status === 'running'
                const agentDone =
                  derived.agents[t.agentId]?.status === 'done'
                const isFresh = freshKey === t.key
                return (
                  <button
                    key={t.key}
                    role="tab"
                    aria-selected={isActive}
                    className={`report-tab ${isActive ? 'active' : ''} ${isFresh ? 'fresh' : ''}`}
                    onClick={() => setPinnedKey(t.key)}
                  >
                    <span>{t.label}</span>
                    {agentRunning ? (
                      <span className="live-pip" aria-label="streaming" />
                    ) : agentDone ? (
                      <span className="done-tick" aria-label="done">
                        ✓
                      </span>
                    ) : null}
                  </button>
                )
              })}
            </div>
          ) : null}

          <div className="es-panel-body">
            <ReportBody
              key={activeKey ?? 'empty'}
              markdown={activeContent}
              ticker={run.ticker}
              streaming
              live={activeIsLive}
            />
          </div>
        </div>

        {/* Activity */}
        <div className="es-card">
          <div className="es-panel-head">
            <span>Activity</span>
            <div style={{ flex: 1 }} />
            <span className="es-pill run" style={{ fontSize: 10 }}>
              <span className="es-dot pulse" />
              live
            </span>
          </div>
          <div className="es-panel-body" style={{ padding: '8px 0' }}>
            {derived.activity.length === 0 && (
              <div
                style={{
                  padding: '14px 16px',
                  color: 'var(--text-3)',
                  fontSize: 12,
                }}
              >
                Waiting for the first event…
              </div>
            )}
            {[...derived.activity].reverse().map((m) => (
              <div
                key={m.seq}
                style={{
                  padding: '10px 14px',
                  borderBottom: '1px solid var(--line-1)',
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    marginBottom: 4,
                  }}
                >
                  <span
                    className="es-mono"
                    style={{ fontSize: 10.5, color: 'var(--text-4)' }}
                  >
                    {m.time}
                  </span>
                  <span
                    style={{
                      fontSize: 10.5,
                      padding: '1px 6px',
                      borderRadius: 3,
                      background:
                        m.type === 'Tool'
                          ? 'var(--accent-bg)'
                          : m.type === 'Section'
                            ? 'var(--ok-bg)'
                            : 'var(--bg-3)',
                      color:
                        m.type === 'Tool'
                          ? 'var(--accent-hi)'
                          : m.type === 'Section'
                            ? 'var(--ok)'
                            : 'var(--text-2)',
                      fontWeight: 600,
                    }}
                  >
                    {m.type}
                  </span>
                  <span
                    style={{
                      fontSize: 11.5,
                      color: 'var(--text-2)',
                      fontWeight: 500,
                    }}
                  >
                    {m.agent}
                  </span>
                </div>
                <div
                  style={{
                    fontSize: 12,
                    color: 'var(--text-3)',
                    lineHeight: 1.5,
                  }}
                >
                  {m.body.length > 180
                    ? m.body.slice(0, 180) + '…'
                    : m.body}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <StatusBar
        toolCalls={derived.toolCalls}
        llmCalls={derived.debateUpdates + derived.activity.length}
        reports={completedReports}
        tokens="—"
        cost="—"
        state="live"
      />
    </div>
  )
}
