/* Simple-builder row model: a flat list of groups (depth 2 max), each row one
   condition. Operands cover const / field / fn / multiplier*fn — anything
   richer (patterns, nested groups >2, fundamentals math) edits as raw JSON. */
import type { ScanCondition, ScanGroup, ScanOperand, Timeframe } from './scanner-types'

export type SimpleOperand =
  | { kind: 'const'; value: number }
  | { kind: 'field'; field: string }
  | { kind: 'fn'; fn: string; of: string; period?: number; component?: string; mult?: number }

export type Row = {
  timeframe: Timeframe
  left: SimpleOperand
  op: string
  right: SimpleOperand
  forN?: number
}

export type BuilderState = { groups: { logic: 'AND' | 'OR'; rows: Row[] }[] }

export const TIMEFRAME_OPTIONS: Timeframe[] = ['5m', '15m', '1h', '1d', '1w', '1mo']
export const OP_OPTIONS = ['>', '<', '>=', '<=', '==', 'crosses_above', 'crosses_below']
export const FIELD_OPTIONS = ['open', 'high', 'low', 'close', 'volume', 'vwap',
  'typical_price', 'gap_pct', 'change_pct', 'body', 'upper_wick', 'lower_wick']
export const FN_OPTIONS = ['SMA', 'EMA', 'WMA', 'HMA', 'VWMA', 'RSI', 'STOCH', 'STOCHRSI',
  'CCI', 'WILLR', 'ROC', 'MOM', 'MACD', 'ADX', 'SUPERTREND', 'PSAR', 'ATR', 'BBANDS',
  'BBWIDTH', 'STDDEV', 'OBV', 'MFI', 'CMF', 'HIGHEST', 'LOWEST', 'SUM', 'AVG']

export function emptyRow(): Row {
  return {
    timeframe: '1d',
    left: { kind: 'field', field: 'close' },
    op: '>',
    right: { kind: 'const', value: 100 },
  }
}

function operandToAst(o: SimpleOperand): ScanOperand {
  if (o.kind === 'const') return { const: o.value }
  if (o.kind === 'field') return { field: o.field }
  const fn: ScanOperand = { fn: o.fn, of: o.of, period: o.period }
  if (o.component) fn.component = o.component
  if (o.period === undefined) delete fn.period
  if (o.mult !== undefined && o.mult !== 1)
    return { expr: '*', args: [{ const: o.mult }, fn] }
  return fn
}

function operandFromAst(o: ScanOperand): SimpleOperand | null {
  if (o.bars_ago) return null
  if (o.const !== undefined) return { kind: 'const', value: o.const }
  if (o.field !== undefined) return { kind: 'field', field: o.field }
  if (o.fn !== undefined) {
    if (o.of !== undefined && typeof o.of !== 'string') return null
    const out: SimpleOperand = { kind: 'fn', fn: o.fn, of: (o.of as string) ?? 'close' }
    if (o.period !== undefined) out.period = o.period
    if (o.component !== undefined) out.component = o.component
    if (o.params && Object.keys(o.params).length) return null
    return out
  }
  if (o.expr === '*' && o.args?.length === 2 && o.args[0].const !== undefined) {
    const inner = operandFromAst(o.args[1])
    if (inner?.kind === 'fn') return { ...inner, mult: o.args[0].const }
  }
  return null
}

function conditionToAst(r: Row): ScanCondition {
  const c: ScanCondition = {
    timeframe: r.timeframe,
    left: operandToAst(r.left),
    op: r.op,
    right: operandToAst(r.right),
  }
  if (r.forN) c.for_n_bars = r.forN
  return c
}

function conditionFromAst(c: ScanCondition): Row | null {
  if (!c.op || !c.right) return null // pattern conditions → JSON tab
  const left = operandFromAst(c.left)
  const right = operandFromAst(c.right)
  if (!left || !right) return null
  const row: Row = { timeframe: c.timeframe, left, op: c.op, right }
  if (c.for_n_bars) row.forN = c.for_n_bars
  return row
}

export function rowsToAst(state: BuilderState): ScanGroup {
  const groups = state.groups.filter((g) => g.rows.length)
  if (groups.length === 1)
    return { logic: groups[0].logic, children: groups[0].rows.map(conditionToAst) }
  return {
    logic: 'AND',
    children: groups.map((g) =>
      g.rows.length === 1
        ? conditionToAst(g.rows[0])
        : { logic: g.logic, children: g.rows.map(conditionToAst) }),
  }
}

/** Canonical compact-JSON form of a definition, normalized through the rows
 *  round-trip whenever the shape is row-representable (astToRows/rowsToAst
 *  fill in defaults — e.g. a bare `{ fn: 'MACD' }` operand gains
 *  `of: 'close'` — so a raw API definition and its post-edit reconstruction
 *  can differ textually with zero actual edits). Shapes the row builder
 *  can't represent (patterns, deep nesting) fall back to a raw compact
 *  stringify, since there's no normalized form to round-trip through.
 *
 *  Used to detect whether a loaded filter has actually been edited —
 *  compare `canonicalScanJson(original)` against
 *  `canonicalScanJson(current)` rather than raw JSON.stringify. */
export function canonicalScanJson(def: ScanGroup): string {
  const rows = astToRows(def)
  return JSON.stringify(rows ? rowsToAst(rows) : def)
}

const OP_LABELS: Record<string, string> = {
  '>': '>', '<': '<', '>=': '>=', '<=': '<=', '==': '=',
  crosses_above: 'crosses above', crosses_below: 'crosses below',
}

function operandLabel(o: SimpleOperand): string {
  if (o.kind === 'const') return String(o.value)
  if (o.kind === 'field') return o.field
  // `of` is only shown when it isn't the 'close' default, keeping the
  // common case ("SMA(200)") short — see describeRow's doc comment.
  const args = [o.of !== 'close' ? o.of : null, o.period !== undefined ? String(o.period) : null]
    .filter((a): a is string => a !== null)
  let base = args.length ? `${o.fn}(${args.join(',')})` : o.fn
  // Multi-line indicators (MACD line vs. signal, STOCH %K vs. %D) share one
  // `fn` but pick out a sub-series via `component` — without this, both
  // read identically in the chip.
  if (o.component) base += `.${o.component}`
  return o.mult !== undefined && o.mult !== 1 ? `${o.mult}×${base}` : base
}

/** Human label for a condition row, used as an inline chip in the filter
 *  panel's collapsed summary bar, e.g. "close > SMA(200) · 1d" or
 *  "volume > 2×SMA(volume,20) · 15m". A `for_n_bars` count renders as a
 *  trailing "for Nb". */
export function describeRow(row: Row): string {
  const op = OP_LABELS[row.op] ?? row.op
  const forSuffix = row.forN ? ` for ${row.forN}b` : ''
  return `${operandLabel(row.left)} ${op} ${operandLabel(row.right)}${forSuffix} · ${row.timeframe}`
}

/** The scanner-workbench's "Liquid only" floor: 20d avg volume >= 100k on
 *  the daily timeframe. Applied at run time only (see withLiquidityFloor)
 *  — never merged into a saved/edited definition. */
const LIQUIDITY_FLOOR_CONDITION: ScanCondition = {
  timeframe: '1d',
  left: { fn: 'SMA', of: 'volume', period: 20 },
  op: '>',
  right: { const: 100000 },
}

/** Wraps `def` with the liquidity floor condition (AND'd alongside it) when
 *  `on` is true; returns `def` unchanged otherwise. Pure — callers apply
 *  this only to the payload sent to previewScanner, never to what gets
 *  saved or shown as the editable definition. */
export function withLiquidityFloor(def: ScanGroup, on: boolean): ScanGroup {
  if (!on) return def
  return { logic: 'AND', children: [def, LIQUIDITY_FLOOR_CONDITION] }
}

export function astToRows(def: ScanGroup): BuilderState | null {
  const groups: BuilderState['groups'] = []
  const top: Row[] = []
  for (const child of def.children) {
    if ('logic' in child) {
      if (def.logic !== 'AND') return null
      const rows: Row[] = []
      for (const inner of child.children) {
        if ('logic' in inner) return null // depth > 2
        const row = conditionFromAst(inner)
        if (!row) return null
        rows.push(row)
      }
      groups.push({ logic: child.logic, rows })
    } else {
      const row = conditionFromAst(child)
      if (!row) return null
      top.push(row)
    }
  }
  if (top.length) groups.unshift({ logic: def.logic, rows: top })
  if (!groups.length) return null
  return { groups }
}
