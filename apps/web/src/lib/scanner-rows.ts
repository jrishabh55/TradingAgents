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
