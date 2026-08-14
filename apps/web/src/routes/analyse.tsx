import { createFileRoute } from '@tanstack/react-router'
import { FlowLanding } from '#/components/flow/FlowLanding'

/** The run-an-analysis page (formerly the landing page at `/`). Accepts
 *  ?ticker=RELIANCE.NS so the scanner's Analyse links can prefill the form. */
export const Route = createFileRoute('/analyse')({
  validateSearch: (s: Record<string, unknown>): { ticker?: string } => ({
    ticker: typeof s.ticker === 'string' && s.ticker ? s.ticker : undefined,
  }),
  component: AnalysePage,
})

function AnalysePage() {
  const { ticker } = Route.useSearch()
  return <FlowLanding initialTicker={ticker} />
}
