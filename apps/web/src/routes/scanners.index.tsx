import { createFileRoute, Link } from '@tanstack/react-router'
import { ChevronDown } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  blankFilter, FilterPanel, filterFromNl, filterFromScanner, type ActiveFilter,
} from '#/components/scanner/FilterPanel'
import { ResultsTable } from '#/components/scanner/ResultsTable'
import { Topbar } from '#/components/shared/Topbar'
import { Button } from '#/components/ui/button'
import {
  Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList,
} from '#/components/ui/command'
import { Input } from '#/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '#/components/ui/popover'
import { api } from '#/lib/api'
import { fuzzyMatch } from '#/lib/fuzzy'
import { withLiquidityFloor } from '#/lib/scanner-rows'
import type { ScannerStatus, ScanResult, ScannerSummary } from '#/lib/scanner-types'

export const Route = createFileRoute('/scanners/')({
  /* Client-only: a relative fetch('/api/…') has no origin during SSR
     ("Invalid URL"). */
  ssr: false,
  loader: async () => {
    return { scanners: await api.listScanners() }
  },
  component: ScannersPage,
})

/** DD Mon, e.g. "13 Aug" — null renders as "backfilling…" (no bars ingested
 *  yet for that timeframe). */
function fmtDay(ts: string | null): string {
  if (!ts) return 'backfilling…'
  return new Date(ts).toLocaleDateString(undefined, { day: '2-digit', month: 'short' })
}

/** HH:MM in the viewer's local time. */
function fmtTime(ts: string | null): string {
  if (!ts) return 'backfilling…'
  return new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

/** The freshest intraday bar across 5m/15m/1h — whichever has ingested
 *  most recently, since not every deployment necessarily runs all three. */
function latestIntraday(status: ScannerStatus): string | null {
  const ts = (['5m', '15m', '1h'] as const)
    .map((tf) => status.latest[tf])
    .filter((v): v is string => v != null)
    .sort()
  return ts.at(-1) ?? null
}

function ScannersPage() {
  const { scanners } = Route.useLoaderData()
  const [items, setItems] = useState<ScannerSummary[]>(scanners)
  const [query, setQuery] = useState('')
  const [nlBusy, setNlBusy] = useState(false)
  const [filter, setFilter] = useState<ActiveFilter | null>(null)
  const [result, setResult] = useState<{ label: string; data: ScanResult } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [manageOpen, setManageOpen] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickerQuery, setPickerQuery] = useState('')
  const [previewing, setPreviewing] = useState(false)
  const [status, setStatus] = useState<ScannerStatus | null>(null)
  const resultsRef = useRef<HTMLElement>(null)
  // Guards against rapid successive selectScanner calls resolving out of
  // order (click scanner A, then B, before A's preview response lands) —
  // only the response matching the latest request is applied.
  const selectSeqRef = useRef(0)

  /* Results render below the filter panel — off-screen once the panel and
     manage section grow. Without this, a completed scan looks like nothing
     happened. */
  useEffect(() => {
    if (result) resultsRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [result])

  function refreshStatus() {
    api.scannerStatus().then(setStatus).catch(() => {
      /* Data-freshness line is informational — leave it blank rather than
         surface an error banner over the whole workbench. */
    })
  }

  // On mount, and again after every successful run — the workbench's "last
  // data refresh" line should track the most recent scan, not just the
  // page load.
  useEffect(() => { refreshStatus() }, [])

  function applyResult(data: ScanResult, label: string) {
    setResult({ label, data })
    refreshStatus()
  }

  // cmdk's built-in `shouldFilter` scores a query against each item's own
  // `value` text; the picker turns that off too and filters by hand so the
  // same fuzzyMatch behavior applies to both name and description.
  const prebuiltHits = useMemo(
    () => items.filter((s) => s.prebuilt && fuzzyMatch(pickerQuery, `${s.name} ${s.description}`)),
    [items, pickerQuery],
  )
  const mineHits = useMemo(
    () => items.filter((s) => !s.prebuilt && fuzzyMatch(pickerQuery, `${s.name} ${s.description}`)),
    [items, pickerQuery],
  )
  const allMine = useMemo(() => items.filter((s) => !s.prebuilt), [items])
  const trimmedQuery = query.trim()

  async function selectScanner(s: ScannerSummary) {
    setPickerOpen(false); setPickerQuery(''); setError(null)
    // Collapsed from the moment the ActiveFilter is created — chips + name
    // render immediately, with no expanded-then-collapsed flash while the
    // auto-preview below is still in flight (see FilterPanel::ActiveFilter
    // doc comment on `collapsed`).
    const filter = filterFromScanner(s, { collapsed: true })
    setFilter(filter)
    const seq = ++selectSeqRef.current
    setPreviewing(true)
    try {
      // Auto-preview applies the liquidity floor too — same as a manual
      // Run in FilterPanel — so the initial result a user sees on
      // selecting a saved scanner already matches what "Run" would show.
      const data = await api.previewScanner(withLiquidityFloor(s.definition, filter.liquidOnly))
      if (seq === selectSeqRef.current) {
        applyResult(data, s.name)
        setFilter((prev) => (prev ? { ...prev, matchCount: data.matches.length } : prev))
      }
    } catch (e) {
      if (seq === selectSeqRef.current) setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (seq === selectSeqRef.current) setPreviewing(false)
    }
  }

  async function generateFromQuery() {
    if (!trimmedQuery || nlBusy) return
    setNlBusy(true); setError(null)
    try {
      const { definition, explanation } = await api.nlScanner(trimmedQuery)
      setFilter(filterFromNl(trimmedQuery, definition, explanation))
      setQuery('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setNlBusy(false)
    }
  }

  function startBlank() {
    setFilter(blankFilter())
    setQuery('')
  }

  function handleSaved(scanner: ScannerSummary, wasUpdate: boolean) {
    setItems((prev) => (wasUpdate
      ? prev.map((s) => (s.id === scanner.id ? scanner : s))
      : [...prev, scanner]))
    setFilter(filterFromScanner(scanner))
  }

  function clearFilter() {
    setFilter(null)
    setResult(null)
  }

  async function remove(s: ScannerSummary) {
    await api.deleteScanner(s.id)
    setItems((prev) => prev.filter((i) => i.id !== s.id))
    // The result and filter panel are showing whatever the active filter
    // last ran — if that filter is the scanner being deleted, both go
    // stale together.
    if (filter?.origin.kind === 'mine' && filter.origin.id === s.id) clearFilter()
  }

  return (
    <div className="min-h-screen">
      <Topbar state="idle" />
      <main className="mx-auto w-full max-w-[1800px] space-y-6 px-6 py-4">
        <h1 className="text-2xl font-bold">Scanners</h1>

        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Popover open={pickerOpen} onOpenChange={(o) => { setPickerOpen(o); if (!o) setPickerQuery('') }}>
              <PopoverTrigger asChild>
                <Button variant="outline" size="sm" className="gap-1.5">
                  Load scanner…
                  <ChevronDown className="size-3.5 opacity-60" />
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-80 p-0" align="start">
                <Command shouldFilter={false}>
                  <CommandInput placeholder="Search scanners…"
                    value={pickerQuery} onValueChange={setPickerQuery} />
                  <CommandList>
                    {!prebuiltHits.length && !mineHits.length && (
                      <CommandEmpty>No scanners match.</CommandEmpty>
                    )}
                    {!!prebuiltHits.length && (
                      <CommandGroup heading="Prebuilt">
                        {prebuiltHits.map((s) => (
                          <CommandItem key={s.id} value={s.id} onSelect={() => selectScanner(s)}>
                            {s.name}
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    )}
                    {!!mineHits.length && (
                      <CommandGroup heading="Mine">
                        {mineHits.map((s) => (
                          <CommandItem key={s.id} value={s.id} onSelect={() => selectScanner(s)}>
                            {s.name}
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    )}
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
          </div>

          <div className="flex flex-wrap items-center gap-2 rounded-[var(--r-lg)] border border-[var(--line-1)] bg-[var(--bg-1)] p-2 shadow-[var(--shadow-1)]">
            <Input
              className="min-w-64 flex-1"
              placeholder="Describe a filter to generate, e.g. &quot;RSI above 60 and price above 200 SMA&quot;…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') generateFromQuery() }}
            />
            <Button size="sm" onClick={generateFromQuery} disabled={!trimmedQuery || nlBusy}>
              {nlBusy ? 'Generating…' : 'Generate'}
            </Button>
            <Button size="sm" variant="outline" onClick={startBlank}>
              New blank filter
            </Button>
          </div>

          {status && (
            <p className="es-mono px-1 text-xs text-muted-foreground">
              Data: daily to {fmtDay(status.latest['1d'])} · intraday to{' '}
              {fmtTime(latestIntraday(status))} (15-min delayed) · {status.universe} stocks
            </p>
          )}
        </div>

        {error && <p className="text-sm text-[var(--err)]">{error}</p>}

        {filter && (
          <FilterPanel filter={filter} onChange={setFilter} onClear={clearFilter}
            onSaved={handleSaved} onResult={applyResult} previewing={previewing} />
        )}

        <section className="space-y-2">
          <Button variant="ghost" size="sm" onClick={() => setManageOpen((v) => !v)}>
            {manageOpen ? 'Hide' : 'Manage'} my scanners ({allMine.length})
          </Button>
          {manageOpen && (
            <div className="space-y-1 rounded-[var(--r-lg)] border border-[var(--line-1)] bg-[var(--bg-1)] p-3 shadow-[var(--shadow-1)]">
              {allMine.map((s) => (
                <div key={s.id}
                  className="flex items-center justify-between gap-2 border-b py-2 last:border-b-0">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{s.name}</p>
                    <p className="truncate text-xs text-muted-foreground">{s.description}</p>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <Button size="sm" variant="outline" asChild>
                      <Link to="/scanners/$id/edit" params={{ id: s.id }}>Edit</Link>
                    </Button>
                    <Button size="sm" variant="destructive" onClick={() => remove(s)}>Delete</Button>
                  </div>
                </div>
              ))}
              {!allMine.length && <p className="text-sm text-muted-foreground">None yet.</p>}
            </div>
          )}
        </section>

        {previewing && !result && (
          <section className="py-2">
            <span className="es-pill run">
              <span className="es-dot pulse" />
              Scanning…
            </span>
          </section>
        )}
        {result && (
          <section ref={resultsRef} className="scroll-mt-4 space-y-3">
            <div className="flex items-center gap-3">
              <div className="space-y-0.5">
                <div className="es-team-label">Results</div>
                <h2 className="text-lg font-semibold">
                  {result.label} ({result.data.matches.length})
                </h2>
              </div>
              {previewing && (
                <span className="es-pill run">
                  <span className="es-dot pulse" />
                  Scanning…
                </span>
              )}
            </div>
            {/* Stale table dims while the next scanner's preview is in
                flight — switching scanners must never look like nothing
                happened. */}
            <div className={previewing ? 'pointer-events-none opacity-40 transition-opacity' : 'transition-opacity'}>
              <ResultsTable result={result.data} />
            </div>
          </section>
        )}

        <footer className="mt-8 border-t pt-4 text-xs text-muted-foreground">
          Drishti is provided &quot;as is&quot;, without warranty of any kind. Nothing here is
          investment advice or a recommendation to buy or sell any security. Market data may be
          delayed or inaccurate. You are solely responsible for your trading decisions — the
          authors accept no liability for losses arising from use of this software.
        </footer>
      </main>
    </div>
  )
}
