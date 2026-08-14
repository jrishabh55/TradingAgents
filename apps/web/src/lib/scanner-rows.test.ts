import { describe, expect, it } from 'vitest'
import {
  astToRows, canonicalScanJson, describeRow, emptyRow, rowsToAst, type Row,
} from './scanner-rows'
import type { ScanGroup } from './scanner-types'

const SIMPLE: ScanGroup = {
  logic: 'AND',
  children: [
    { timeframe: '1d', left: { fn: 'EMA', of: 'close', period: 20 },
      op: 'crosses_above', right: { fn: 'EMA', of: 'close', period: 50 } },
    { timeframe: '15m', left: { field: 'volume' }, op: '>',
      right: { expr: '*', args: [{ const: 2 }, { fn: 'SMA', of: 'volume', period: 20 }] } },
  ],
}

describe('rows <-> ast', () => {
  it('round-trips a simple definition', () => {
    const rows = astToRows(SIMPLE)
    expect(rows).not.toBeNull()
    expect(rowsToAst(rows!)).toEqual(SIMPLE)
  })

  it('round-trips one nested OR group', () => {
    const def: ScanGroup = {
      logic: 'AND',
      children: [
        SIMPLE.children[0],
        { logic: 'OR', children: [SIMPLE.children[1], SIMPLE.children[0]] },
      ],
    }
    const rows = astToRows(def)
    expect(rows).not.toBeNull()
    expect(rowsToAst(rows!)).toEqual(def)
  })

  it('returns null for shapes the simple builder cannot edit', () => {
    const deep: ScanGroup = {
      logic: 'AND',
      children: [{ logic: 'OR', children: [{ logic: 'AND', children: [SIMPLE.children[0]] }] }],
    }
    expect(astToRows(deep)).toBeNull()
    expect(astToRows({ logic: 'AND', children: [
      { timeframe: '1d', left: { pattern: 'doji' } }] })).toBeNull()
  })

  it('emptyRow produces a valid condition', () => {
    const ast = rowsToAst({ groups: [{ logic: 'AND', rows: [emptyRow()] }] })
    expect(ast.children.length).toBe(1)
  })
})

describe('canonicalScanJson', () => {
  // Regression coverage for a real bug: prebuilt scanners like
  // macd_bull_cross and supertrend_flip use `fn` operands without an `of`
  // (defaults to 'close' on round-trip) or with only a `period` set — a
  // naive raw-vs-round-tripped JSON.stringify comparison flagged these as
  // "edited" the moment they loaded, with zero actual user edits.
  it('is stable across the rows round-trip for an fn operand missing `of`', () => {
    const def: ScanGroup = {
      logic: 'AND',
      children: [{ timeframe: '1d', left: { fn: 'MACD' }, op: '>', right: { const: 0 } }],
    }
    const rows = astToRows(def)
    expect(rows).not.toBeNull()
    expect(canonicalScanJson(rowsToAst(rows!))).toBe(canonicalScanJson(def))
  })

  it('is stable across the rows round-trip for an fn operand with only a period set', () => {
    const def: ScanGroup = {
      logic: 'AND',
      children: [{
        timeframe: '1d', left: { fn: 'RSI', of: 'close', period: 14 },
        op: '<', right: { const: 30 },
      }],
    }
    const rows = astToRows(def)
    expect(rows).not.toBeNull()
    expect(canonicalScanJson(rowsToAst(rows!))).toBe(canonicalScanJson(def))
  })

  it('load with no edit never appears dirty, for prebuilt-like shapes', () => {
    const shapes: ScanGroup[] = [
      { logic: 'AND', children: [{ timeframe: '1d', left: { fn: 'MACD' }, op: 'crosses_above', right: { const: 0 } }] },
      { logic: 'AND', children: [{ timeframe: '1d', left: { fn: 'SUPERTREND', period: 10 }, op: '<', right: { field: 'close' } }] },
    ]
    for (const def of shapes) {
      const rows = astToRows(def)
      expect(rows).not.toBeNull()
      // Simulates FilterPanel: originalJson at load time vs. the "current"
      // side reconstructed from rows with no edits applied.
      const originalJson = canonicalScanJson(def)
      const currentJson = canonicalScanJson(rowsToAst(rows!))
      expect(currentJson).toBe(originalJson)
    }
  })

  it('falls back to raw compact JSON for shapes the row builder cannot represent', () => {
    const def: ScanGroup = { logic: 'AND', children: [{ timeframe: '1d', left: { pattern: 'doji' } }] }
    expect(astToRows(def)).toBeNull()
    expect(canonicalScanJson(def)).toBe(JSON.stringify(def))
  })
})

describe('describeRow', () => {
  it('labels a field-vs-fn condition, omitting the default `of: close`', () => {
    const row: Row = {
      timeframe: '1d', left: { kind: 'field', field: 'close' }, op: '>',
      right: { kind: 'fn', fn: 'SMA', of: 'close', period: 200 },
    }
    expect(describeRow(row)).toBe('close > SMA(200) · 1d')
  })

  it('labels a fn-vs-const condition', () => {
    const row: Row = {
      timeframe: '1d', left: { kind: 'fn', fn: 'RSI', of: 'close', period: 14 }, op: '>',
      right: { kind: 'const', value: 60 },
    }
    expect(describeRow(row)).toBe('RSI(14) > 60 · 1d')
  })

  it('labels a fn operand with a multiplier, keeping a non-close `of`', () => {
    const row: Row = {
      timeframe: '15m', left: { kind: 'field', field: 'volume' }, op: '>',
      right: { kind: 'fn', fn: 'SMA', of: 'volume', period: 20, mult: 2 },
    }
    expect(describeRow(row)).toBe('volume > 2×SMA(volume,20) · 15m')
  })

  it('appends the component for multi-line indicators (MACD signal)', () => {
    const row: Row = {
      timeframe: '1d', left: { kind: 'fn', fn: 'MACD', of: 'close', component: 'signal' }, op: '>',
      right: { kind: 'const', value: 0 },
    }
    expect(describeRow(row)).toContain('.signal')
    expect(describeRow(row)).toBe('MACD.signal > 0 · 1d')
  })

  it('appends a for_n_bars suffix', () => {
    const row: Row = {
      timeframe: '1d', left: { kind: 'field', field: 'close' }, op: '>',
      right: { kind: 'const', value: 100 }, forN: 3,
    }
    expect(describeRow(row)).toBe('close > 100 for 3b · 1d')
  })
})
