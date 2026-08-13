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

  return (
    <div className="space-y-2">
      <p className="text-sm text-muted-foreground">
        {result.matches.length} of {result.universe} stocks · data as of{' '}
        {result.data_as_of ? new Date(result.data_as_of).toLocaleString() : '—'} (delayed)
      </p>
      <div className="overflow-x-auto rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              {[...BASE_COLS.map((c) => ({ key: String(c.key), label: c.label })),
                ...valueCols.map((k) => ({ key: k, label: k }))].map((c) => (
                <TableHead key={c.key} onClick={() => onSort(c.key)}
                  className="cursor-pointer select-none whitespace-nowrap">
                  {c.label}{sortKey === c.key ? (desc ? ' ↓' : ' ↑') : ''}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((m) => (
              <TableRow key={m.symbol} className="cursor-pointer"
                onClick={() => setChartSymbol(m.symbol)}>
                <TableCell className="font-medium">{m.symbol}</TableCell>
                <TableCell className="max-w-48 truncate">{m.name}</TableCell>
                <TableCell>{m.sector ?? '—'}</TableCell>
                <TableCell>{m.close?.toLocaleString() ?? '—'}</TableCell>
                <TableCell className={m.change_pct != null && m.change_pct < 0
                  ? 'text-red-500' : 'text-emerald-500'}>
                  {m.change_pct != null ? `${m.change_pct.toFixed(2)}%` : '—'}
                </TableCell>
                <TableCell>{m.volume?.toLocaleString() ?? '—'}</TableCell>
                <TableCell>{m.rvol?.toFixed(2) ?? '—'}</TableCell>
                {valueCols.map((k) => (
                  <TableCell key={k}>{m.values[k]?.toFixed(2) ?? '—'}</TableCell>
                ))}
              </TableRow>
            ))}
            {!rows.length && (
              <TableRow><TableCell colSpan={7 + valueCols.length}
                className="py-8 text-center text-muted-foreground">
                No stocks match right now.
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <Dialog open={!!chartSymbol} onOpenChange={(o) => !o && setChartSymbol(null)}>
        <DialogContent className="max-w-4xl">
          <DialogTitle>{chartSymbol} — live chart (TradingView)</DialogTitle>
          {chartSymbol && (
            <iframe
              title={`chart-${chartSymbol}`}
              src={`https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(`NSE:${chartSymbol}`)}&interval=D&hidesidetoolbar=1&theme=dark&style=1&locale=en`}
              className="h-[480px] w-full rounded-md border-0"
            />
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
