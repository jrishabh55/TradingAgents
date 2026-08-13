export type Timeframe = '5m' | '15m' | '1h' | '1d' | '1w' | '1mo'

export type ScanOperand = {
  const?: number
  const_str?: string
  field?: string
  fn?: string
  of?: string | ScanOperand
  period?: number
  params?: Record<string, number>
  component?: string
  expr?: string
  args?: ScanOperand[]
  fundamental?: string
  meta?: string
  pattern?: string
  bars_ago?: number
}

export type ScanCondition = {
  timeframe: Timeframe
  left: ScanOperand
  op?: string
  right?: ScanOperand
  for_n_bars?: number
}

export type ScanGroup = {
  logic: 'AND' | 'OR'
  children: (ScanGroup | ScanCondition)[]
}

export type ScannerSummary = {
  id: string
  name: string
  description: string
  prebuilt: boolean
  definition: ScanGroup
  created_at: string
  updated_at: string
}

export type ScanMatch = {
  symbol: string
  name: string
  sector: string | null
  close: number | null
  change_pct: number | null
  volume: number | null
  rvol: number | null
  values: Record<string, number | null>
}

export type ScanResult = {
  data_as_of: string
  universe: number
  matches: ScanMatch[]
}
