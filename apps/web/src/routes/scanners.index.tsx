import { createFileRoute, Link } from '@tanstack/react-router'
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  blankFilter, FilterPanel, filterFromNl, filterFromScanner, type ActiveFilter,
} from '#/components/scanner/FilterPanel'
import { ResultsTable } from '#/components/scanner/ResultsTable'
import { Topbar } from '#/components/shared/Topbar'
import { Badge } from '#/components/ui/badge'
import { Button } from '#/components/ui/button'
import {
  Command, CommandGroup, CommandInput, CommandItem, CommandList,
} from '#/components/ui/command'
import { api } from '#/lib/api'
import { fuzzyMatch } from '#/lib/fuzzy'
import { withLiquidityFloor } from '#/lib/scanner-rows'
import type { ScanResult, ScannerSummary } from '#/lib/scanner-types'

export const Route = createFileRoute('/scanners/')({
  /* Client-only: a relative fetch('/api/…') has no origin during SSR
     ("Invalid URL"). */
  ssr: false,
  loader: async () => {
    return { scanners: await api.listScanners() }
  },
  component: ScannersPage,
})

function ScannersPage() {
  const { scanners } = Route.useLoaderData()
  const [items, setItems] = useState<ScannerSummary[]>(scanners)
  const [query, setQuery] = useState('')
  const [nlBusy, setNlBusy] = useState(false)
  const [filter, setFilter] = useState<ActiveFilter | null>(null)
  const [result, setResult] = useState<{ label: string; data: ScanResult } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [manageOpen, setManageOpen] = useState(false)
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

  // cmdk's built-in `shouldFilter` scores a query against each item's own
  // `value` text — which would hide "New blank filter" the moment a typed
  // query's letters aren't a subsequence of that fixed label. The footer
  // actions must always be visible while there's a query, so filtering is
  // done by hand and cmdk's own matching is turned off entirely.
  const prebuilt = useMemo(
    () => items.filter((s) => s.prebuilt && fuzzyMatch(query, `${s.name} ${s.description}`)),
    [items, query],
  )
  const mine = useMemo(
    () => items.filter((s) => !s.prebuilt && fuzzyMatch(query, `${s.name} ${s.description}`)),
    [items, query],
  )
  const allMine = useMemo(() => items.filter((s) => !s.prebuilt), [items])
  const trimmedQuery = query.trim()

  async function selectScanner(s: ScannerSummary) {
    setQuery(''); setError(null)
    const filter = filterFromScanner(s)
    setFilter(filter)
    const seq = ++selectSeqRef.current
    try {
      // Auto-preview applies the liquidity floor too — same as a manual
      // Run in FilterPanel — so the initial result a user sees on
      // selecting a saved scanner already matches what "Run" would show.
      const data = await api.previewScanner(withLiquidityFloor(s.definition, filter.liquidOnly))
      if (seq === selectSeqRef.current) {
        setResult({ label: s.name, data })
        // Selecting a saved scanner previews it right away — treat that
        // like a successful Run and collapse the panel to its summary bar.
        setFilter((prev) => (prev ? { ...prev, collapsed: true, matchCount: data.matches.length } : prev))
      }
    } catch (e) {
      if (seq === selectSeqRef.current) setError(e instanceof Error ? e.message : String(e))
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
      <main className="mx-auto max-w-6xl space-y-6 p-4">
        <h1 className="text-2xl font-bold">Scanners</h1>

        <Command className="rounded-lg border" shouldFilter={false}>
          <CommandInput placeholder="Search scanners, or describe one to generate a filter…"
            value={query} onValueChange={setQuery} />
          <CommandList>
            {!prebuilt.length && !mine.length && !trimmedQuery && (
              <p className="py-6 text-center text-sm text-muted-foreground">
                No scanners yet — describe one below or start blank.
              </p>
            )}
            {!!prebuilt.length && (
              <CommandGroup heading="Prebuilt">
                {prebuilt.map((s) => (
                  <CommandItem key={s.id} value={s.id} onSelect={() => selectScanner(s)}>
                    <span>{s.name}</span>
                    <Badge variant="secondary" className="ml-2">prebuilt</Badge>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
            {!!mine.length && (
              <CommandGroup heading="Mine">
                {mine.map((s) => (
                  <CommandItem key={s.id} value={s.id} onSelect={() => selectScanner(s)}>
                    {s.name}
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
            {!!trimmedQuery && (
              <CommandGroup heading="Or">
                <CommandItem value={`__generate__${trimmedQuery}`} disabled={nlBusy}
                  onSelect={generateFromQuery}>
                  {nlBusy ? 'Generating…' : `Generate filter from: "${trimmedQuery}"`}
                </CommandItem>
                <CommandItem value="__blank__" onSelect={startBlank}>
                  New blank filter
                </CommandItem>
              </CommandGroup>
            )}
          </CommandList>
        </Command>

        {error && <p className="text-sm text-red-500">{error}</p>}

        {filter && (
          <FilterPanel filter={filter} onChange={setFilter} onClear={clearFilter}
            onSaved={handleSaved} onResult={(data, label) => setResult({ label, data })} />
        )}

        <section className="space-y-2">
          <Button variant="ghost" size="sm" onClick={() => setManageOpen((v) => !v)}>
            {manageOpen ? 'Hide' : 'Manage'} my scanners ({allMine.length})
          </Button>
          {manageOpen && (
            <div className="space-y-1 rounded-md border p-3">
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

        {result && (
          <section ref={resultsRef} className="scroll-mt-4 space-y-3">
            <div className="space-y-0.5">
              <div className="es-team-label">Results</div>
              <h2 className="text-lg font-semibold">
                {result.label} ({result.data.matches.length})
              </h2>
            </div>
            <ResultsTable result={result.data} />
          </section>
        )}
      </main>
    </div>
  )
}
