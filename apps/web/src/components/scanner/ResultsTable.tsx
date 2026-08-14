import { Link } from '@tanstack/react-router'
import { useMemo, useState } from 'react'
import { Dialog, DialogContent, DialogTitle } from '#/components/ui/dialog'
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from '#/components/ui/table'
import type { ScanMatch, ScanResult } from '#/lib/scanner-types'

const BASE_COLS: { key: keyof ScanMatch; label: string }[] = [
  { key: 'symbol', label: 'Symbol' },
  { key: 'name', label: 'Name' },
  { key: 'sector', label: 'Sector' },
  { key: 'close', label: 'Price' },
  { key: 'change_pct', label: '% Chg' },
  { key: 'volume', label: 'Volume' },
  { key: 'rvol', label: 'RVol' },
]

export function ResultsTable({ result }: { result: ScanResult }) {
  const [sortKey, setSortKey] = useState<string>('change_pct')
  const [desc, setDesc] = useState(true)
  const [chartSymbol, setChartSymbol] = useState<string | null>(null)

  const valueCols = useMemo(
    () => Object.keys(result.matches[0]?.values ?? {}),
    [result],
  )

  const rows = useMemo(() => {
    const get = (m: ScanMatch) =>
      (BASE_COLS.some((c) => c.key === sortKey)
        ? m[sortKey as keyof ScanMatch]
        : m.values[sortKey]) as number | string | null
    return [...result.matches].sort((a, b) => {
      const av = get(a), bv = get(b)
      if (av == null) return 1
      if (bv == null) return -1
      const cmp = typeof av === 'string' ? av.localeCompare(String(bv)) : Number(av) - Number(bv)
      return desc ? -cmp : cmp
    })
  }, [result, sortKey, desc])

  const onSort = (key: string) => {
    if (key === sortKey) setDesc(!desc)
    else { setSortKey(key); setDesc(true) }
  }

  // Numeric columns get right-aligned tabular-nums mono rendering; text
  // columns (symbol/name/sector) stay left-aligned.
  const NUMERIC_BASE = new Set(['close', 'change_pct', 'volume', 'rvol'])

  return (
    <div className="space-y-2">
      <p className="es-mono text-xs text-muted-foreground">
        {result.matches.length} of {result.universe} stocks · data as of{' '}
        {result.data_as_of ? new Date(result.data_as_of).toLocaleString() : '—'} (delayed)
      </p>
      <div className="max-h-[560px] overflow-auto rounded-[var(--r-lg)] border border-[var(--line-1)] bg-[var(--bg-1)] shadow-[var(--shadow-1)]">
        <Table>
          <TableHeader>
            <TableRow>
              {[...BASE_COLS.map((c) => ({
                key: String(c.key),
                label: c.key === 'change_pct' && result.change_tf && result.change_tf !== '1d'
                  ? `% Chg [${result.change_tf}]` : c.label,
              })),
                ...valueCols.map((k) => ({ key: k, label: k }))].map((c) => (
                <TableHead key={c.key} onClick={() => onSort(c.key)}
                  className={`es-team-label sticky top-0 z-10 cursor-pointer select-none whitespace-nowrap bg-card ${
                    NUMERIC_BASE.has(c.key) || !BASE_COLS.some((b) => String(b.key) === c.key)
                      ? 'text-right' : 'text-left'}`}>
                  {c.label}{sortKey === c.key ? (desc ? ' ↓' : ' ↑') : ''}
                </TableHead>
              ))}
              <TableHead className="es-team-label sticky top-0 z-10 select-none whitespace-nowrap bg-card text-right">
                Analyse
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((m) => (
              <TableRow key={m.symbol} className="cursor-pointer"
                onClick={() => setChartSymbol(m.symbol)}>
                <TableCell className="es-mono font-medium text-primary">{m.symbol}</TableCell>
                <TableCell className="max-w-72 truncate text-muted-foreground">{m.name}</TableCell>
                <TableCell className="text-muted-foreground">{m.sector ?? '—'}</TableCell>
                <TableCell className="es-mono text-right tabular-nums">
                  {m.close?.toLocaleString() ?? '—'}
                </TableCell>
                <TableCell className={`es-mono text-right font-medium tabular-nums ${
                  m.change_pct != null && m.change_pct < 0
                    ? 'text-[var(--err)]' : 'text-[var(--ok)]'}`}>
                  {m.change_pct != null ? `${m.change_pct.toFixed(2)}%` : '—'}
                </TableCell>
                <TableCell className="es-mono text-right tabular-nums text-muted-foreground">
                  {m.volume?.toLocaleString() ?? '—'}
                </TableCell>
                <TableCell className="es-mono text-right tabular-nums">
                  {m.rvol?.toFixed(2) ?? '—'}
                </TableCell>
                {valueCols.map((k) => (
                  <TableCell key={k} className="es-mono text-right tabular-nums">
                    {m.values[k]?.toFixed(2) ?? '—'}
                  </TableCell>
                ))}
                <TableCell className="text-right">
                  {/* NSE symbols run as Yahoo tickers (SYMBOL.NS) — see schemas.py */}
                  <Link to="/analyse" search={{ ticker: `${m.symbol}.NS` }}
                    onClick={(e) => e.stopPropagation()}
                    className="text-xs font-medium text-primary underline-offset-2 hover:underline">
                    Analyse →
                  </Link>
                </TableCell>
              </TableRow>
            ))}
            {!rows.length && (
              <TableRow><TableCell colSpan={8 + valueCols.length}
                className="py-8 text-center text-muted-foreground">
                No stocks match right now.
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <Dialog open={!!chartSymbol} onOpenChange={(o) => !o && setChartSymbol(null)}>
        <DialogContent className="sm:max-w-[min(1280px,92vw)]">
          <DialogTitle>{chartSymbol} — live chart (TradingView)</DialogTitle>
          {chartSymbol && (
            <>
              {/* ponytail: NSE data is blocked in TradingView embeds (exchange licensing), BSE isn't — dual listings share the ticker */}
              <iframe
                title={`chart-${chartSymbol}`}
                src={`https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(`BSE:${chartSymbol}`)}&interval=D&hidesidetoolbar=1&theme=dark&style=1&locale=en`}
                className="h-[70vh] w-full rounded-md border-0"
              />
              <a
                href={`https://www.tradingview.com/chart/?symbol=${encodeURIComponent(`NSE:${chartSymbol}`)}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
              >
                BSE chart shown — open NSE chart on TradingView ↗
              </a>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
