/* Static team/agent map that mirrors the CLI's MessageBuffer agent_status
   names (cli/utils.py). Used to render the Pipeline tree before live SSE
   events have arrived. SSE events update agent state by `analyst` / `agent`
   id; this map is the layout. */

export interface AgentNode {
  id: string
  name: string
}
export interface TeamNode {
  id: string
  name: string
  agents: AgentNode[]
}

export const TEAMS: TeamNode[] = [
  {
    id: 'analyst',
    name: 'Analyst Team',
    agents: [
      { id: 'market', name: 'Market Analyst' },
      { id: 'social', name: 'Social Media Analyst' },
      { id: 'news', name: 'News Analyst' },
      { id: 'fundamentals', name: 'Fundamentals Analyst' },
    ],
  },
  {
    id: 'research',
    name: 'Research Team',
    agents: [
      { id: 'bull', name: 'Bull Researcher' },
      { id: 'bear', name: 'Bear Researcher' },
      { id: 'research_manager', name: 'Research Manager' },
    ],
  },
  {
    id: 'trading',
    name: 'Trading Team',
    agents: [{ id: 'trader', name: 'Trader' }],
  },
  {
    id: 'risk',
    name: 'Risk Management',
    agents: [
      { id: 'risky', name: 'Risky Analyst' },
      { id: 'neutral', name: 'Neutral Analyst' },
      { id: 'safe', name: 'Safe Analyst' },
    ],
  },
  {
    id: 'pm',
    name: 'Portfolio Management',
    agents: [{ id: 'portfolio', name: 'Portfolio Manager' }],
  },
]

export type AgentStatus = 'queued' | 'running' | 'done' | 'error'

export const totalAgents = TEAMS.reduce(
  (n, t) => n + t.agents.length,
  0,
)
