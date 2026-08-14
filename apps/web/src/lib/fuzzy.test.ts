import { describe, expect, it } from 'vitest'
import { fuzzyMatch } from './fuzzy'

describe('fuzzyMatch', () => {
  it('matches an exact substring', () => {
    expect(fuzzyMatch('breakout', 'Momentum Breakout Scanner')).toBe(true)
  })

  it('matches out-of-order-free subsequences', () => {
    expect(fuzzyMatch('mbo', 'Momentum Breakout')).toBe(true)
  })

  it('is case-insensitive', () => {
    expect(fuzzyMatch('RSI', 'oversold rsi bounce')).toBe(true)
  })

  it('rejects when characters are out of order', () => {
    expect(fuzzyMatch('obm', 'Momentum Breakout')).toBe(false)
  })

  it('rejects when a character is missing entirely', () => {
    expect(fuzzyMatch('zzz', 'Momentum Breakout')).toBe(false)
  })

  it('treats an empty or whitespace query as matching everything', () => {
    expect(fuzzyMatch('', 'anything')).toBe(true)
    expect(fuzzyMatch('   ', 'anything')).toBe(true)
  })
})
