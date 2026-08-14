import { Trash2 } from 'lucide-react'
import { Button } from '#/components/ui/button'
import { Input } from '#/components/ui/input'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '#/components/ui/select'
import {
  emptyRow, FIELD_OPTIONS, FN_OPTIONS, OP_OPTIONS,
  TIMEFRAME_OPTIONS, type BuilderState, type Row, type SimpleOperand,
} from '#/lib/scanner-rows'

function OperandEditor({ value, onChange, allowMult }: {
  value: SimpleOperand
  onChange: (o: SimpleOperand) => void
  allowMult?: boolean
}) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1">
      <Select value={value.kind} onValueChange={(kind) => onChange(
        kind === 'const' ? { kind: 'const', value: 100 }
          : kind === 'field' ? { kind: 'field', field: 'close' }
            : { kind: 'fn', fn: 'SMA', of: 'close', period: 20 })}>
        <SelectTrigger className="w-[84px] shrink-0 text-xs"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="const">Number</SelectItem>
          <SelectItem value="field">Price/Vol</SelectItem>
          <SelectItem value="fn">Indicator</SelectItem>
        </SelectContent>
      </Select>
      {value.kind === 'const' && (
        <Input type="number" className="w-20 shrink-0 text-xs" value={value.value}
          onChange={(e) => onChange({ kind: 'const', value: Number(e.target.value) })} />
      )}
      {value.kind === 'field' && (
        <Select value={value.field} onValueChange={(field) => onChange({ kind: 'field', field })}>
          <SelectTrigger className="w-28 shrink-0 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>{FIELD_OPTIONS.map((f) =>
            <SelectItem key={f} value={f}>{f}</SelectItem>)}</SelectContent>
        </Select>
      )}
      {value.kind === 'fn' && (
        <>
          {allowMult && (
            <Input type="number" step="0.1" className="w-14 shrink-0 text-xs" placeholder="1x"
              value={value.mult ?? ''} onChange={(e) => onChange({
                ...value, mult: e.target.value ? Number(e.target.value) : undefined })} />
          )}
          <Select value={value.fn} onValueChange={(fn) => onChange({ ...value, fn })}>
            <SelectTrigger className="w-28 shrink-0 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>{FN_OPTIONS.map((f) =>
              <SelectItem key={f} value={f}>{f}</SelectItem>)}</SelectContent>
          </Select>
          <Select value={value.of} onValueChange={(of) => onChange({ ...value, of })}>
            <SelectTrigger className="w-24 shrink-0 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>{FIELD_OPTIONS.map((f) =>
              <SelectItem key={f} value={f}>{f}</SelectItem>)}</SelectContent>
          </Select>
          <Input type="number" className="w-16 shrink-0 text-xs" placeholder="period"
            value={value.period ?? ''} onChange={(e) => onChange({
              ...value, period: e.target.value ? Number(e.target.value) : undefined })} />
        </>
      )}
    </div>
  )
}

/** The condition-row editing grid shared by ScannerBuilder (full /scanners/new
 *  and edit routes) and the scanner-workbench filter panel. Renders nothing
 *  when the AST can't be represented as rows — callers fall back to the raw
 *  JSON editor in that case (see scanner-rows.ts::astToRows). */
export function ConditionRows({ state, onChange }: {
  state: BuilderState
  onChange: (s: BuilderState) => void
}) {
  const updateRow = (gi: number, ri: number, row: Row) => {
    const next = structuredClone(state)
    next.groups[gi].rows[ri] = row
    onChange(next)
  }

  return (
    <div className="space-y-2">
      {state.groups.map((g, gi) => (
        <div key={gi} className="space-y-1.5 rounded-md border bg-muted/30 p-2">
          <div className="flex items-center gap-2 border-b pb-1.5">
            <Select value={g.logic} onValueChange={(logic) => {
              const next = structuredClone(state)
              next.groups[gi].logic = logic as 'AND' | 'OR'
              onChange(next)
            }}>
              <SelectTrigger className="w-[76px] bg-card font-mono text-xs font-bold">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="AND">AND</SelectItem>
                <SelectItem value="OR">OR</SelectItem>
              </SelectContent>
            </Select>
            <span className="es-team-label text-[10px]">Group {gi + 1}</span>
            {state.groups.length > 1 && (
              <Button size="sm" variant="ghost" className="ml-auto h-6 px-2 text-xs" onClick={() => {
                const next = structuredClone(state)
                next.groups.splice(gi, 1)
                onChange(next)
              }}>Remove group</Button>
            )}
          </div>

          <div className="hidden items-center gap-1.5 px-1 sm:flex">
            <span className="es-team-label w-16 shrink-0">Timeframe</span>
            <span className="es-team-label flex-1">Left operand</span>
            <span className="es-team-label w-[108px] shrink-0">Operator</span>
            <span className="es-team-label flex-1">Right operand</span>
            <span className="w-7 shrink-0" />
          </div>

          {g.rows.map((r, ri) => (
            <div key={ri} className="flex flex-wrap items-center gap-1.5 rounded bg-card/70 p-2">
              <Select value={r.timeframe} onValueChange={(timeframe) =>
                updateRow(gi, ri, { ...r, timeframe: timeframe as Row['timeframe'] })}>
                <SelectTrigger className="w-16 shrink-0 text-xs"><SelectValue /></SelectTrigger>
                <SelectContent>{TIMEFRAME_OPTIONS.map((t) =>
                  <SelectItem key={t} value={t}>{t}</SelectItem>)}</SelectContent>
              </Select>
              <OperandEditor value={r.left} onChange={(left) => updateRow(gi, ri, { ...r, left })} />
              <Select value={r.op} onValueChange={(op) => updateRow(gi, ri, { ...r, op })}>
                <SelectTrigger className="w-[108px] shrink-0 font-mono text-[11px]"><SelectValue /></SelectTrigger>
                <SelectContent>{OP_OPTIONS.map((o) =>
                  <SelectItem key={o} value={o}>{o.replace('_', ' ')}</SelectItem>)}</SelectContent>
              </Select>
              <OperandEditor allowMult value={r.right}
                onChange={(right) => updateRow(gi, ri, { ...r, right })} />
              <Button size="icon-sm" variant="ghost" className="ml-auto shrink-0 text-muted-foreground hover:text-destructive"
                aria-label="Remove condition" onClick={() => {
                  const next = structuredClone(state)
                  next.groups[gi].rows.splice(ri, 1)
                  onChange(next)
                }}>
                <Trash2 className="size-3.5" />
              </Button>
            </div>
          ))}
          <Button size="sm" variant="ghost"
            className="w-full border border-dashed text-xs hover:border-primary/40" onClick={() => {
              const next = structuredClone(state)
              next.groups[gi].rows.push(emptyRow())
              onChange(next)
            }}>+ Add condition</Button>
        </div>
      ))}
      <Button size="sm" variant="outline" onClick={() => {
        const next = structuredClone(state)
        next.groups.push({ logic: 'AND', rows: [emptyRow()] })
        onChange(next)
      }}>+ Add filter group</Button>
    </div>
  )
}
