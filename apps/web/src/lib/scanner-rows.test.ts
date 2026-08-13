import { describe, expect, it } from 'vitest'
import { astToRows, emptyRow, rowsToAst } from './scanner-rows'
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
