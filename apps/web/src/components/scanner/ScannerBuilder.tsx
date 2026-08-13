import { useNavigate } from '@tanstack/react-router'
import { useState } from 'react'
import { ResultsTable } from '#/components/scanner/ResultsTable'
import { Button } from '#/components/ui/button'
import { Input } from '#/components/ui/input'
import { Label } from '#/components/ui/label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '#/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '#/components/ui/tabs'
import { Textarea } from '#/components/ui/textarea'
import { api } from '#/lib/api'
import {
  astToRows, emptyRow, FIELD_OPTIONS, FN_OPTIONS, OP_OPTIONS, rowsToAst,
  TIMEFRAME_OPTIONS, type BuilderState, type Row, type SimpleOperand,
} from '#/lib/scanner-rows'
import type { ScanGroup, ScanResult, ScannerSummary } from '#/lib/scanner-types'

function OperandEditor({ value, onChange, allowMult }: {
  value: SimpleOperand
  onChange: (o: SimpleOperand) => void
  allowMult?: boolean
}) {
  return (
    <div className="flex items-center gap-1">
      <Select value={value.kind} onValueChange={(kind) => onChange(
        kind === 'const' ? { kind: 'const', value: 100 }
          : kind === 'field' ? { kind: 'field', field: 'close' }
            : { kind: 'fn', fn: 'SMA', of: 'close', period: 20 })}>
        <SelectTrigger className="w-24"><SelectValue /></SelectTrigger>
        <SelectContent>
          <SelectItem value="const">Number</SelectItem>
          <SelectItem value="field">Price/Vol</SelectItem>
          <SelectItem value="fn">Indicator</SelectItem>
        </SelectContent>
      </Select>
      {value.kind === 'const' && (
        <Input type="number" className="w-24" value={value.value}
          onChange={(e) => onChange({ kind: 'const', value: Number(e.target.value) })} />
      )}
      {value.kind === 'field' && (
        <Select value={value.field} onValueChange={(field) => onChange({ kind: 'field', field })}>
          <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
          <SelectContent>{FIELD_OPTIONS.map((f) =>
            <SelectItem key={f} value={f}>{f}</SelectItem>)}</SelectContent>
        </Select>
      )}
      {value.kind === 'fn' && (
        <>
          {allowMult && (
            <Input type="number" step="0.1" className="w-16" placeholder="1x"
              value={value.mult ?? ''} onChange={(e) => onChange({
                ...value, mult: e.target.value ? Number(e.target.value) : undefined })} />
          )}
          <Select value={value.fn} onValueChange={(fn) => onChange({ ...value, fn })}>
            <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
            <SelectContent>{FN_OPTIONS.map((f) =>
              <SelectItem key={f} value={f}>{f}</SelectItem>)}</SelectContent>
          </Select>
          <Select value={value.of} onValueChange={(of) => onChange({ ...value, of })}>
            <SelectTrigger className="w-28"><SelectValue /></SelectTrigger>
            <SelectContent>{FIELD_OPTIONS.map((f) =>
              <SelectItem key={f} value={f}>{f}</SelectItem>)}</SelectContent>
          </Select>
          <Input type="number" className="w-20" placeholder="period"
            value={value.period ?? ''} onChange={(e) => onChange({
              ...value, period: e.target.value ? Number(e.target.value) : undefined })} />
        </>
      )}
    </div>
  )
}

export function ScannerBuilder({ initial }: { initial: ScannerSummary | null }) {
  const navigate = useNavigate()
  const initialRows = initial ? astToRows(initial.definition) : { groups: [{ logic: 'AND' as const, rows: [emptyRow()] }] }
  const [name, setName] = useState(initial?.name ?? '')
  const [description, setDescription] = useState(initial?.description ?? '')
  const [state, setState] = useState<BuilderState | null>(initialRows)
  const [json, setJson] = useState(() =>
    JSON.stringify(initial?.definition ?? rowsToAst(initialRows!), null, 2))
  const [tab, setTab] = useState(initialRows ? 'builder' : 'json')
  const [nlPrompt, setNlPrompt] = useState('')
  const [nlBusy, setNlBusy] = useState(false)
  const [explanation, setExplanation] = useState<string | null>(null)
  const [preview, setPreview] = useState<ScanResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Invariant: each tab's source of truth is synced on tab exit (see the
  // Tabs onValueChange below) — builder edits are flushed into `json` when
  // leaving the builder tab, and json edits are parsed into `state` when
  // leaving the json tab. So whichever tab is active here already holds the
  // current definition; there's no cross-tab staleness to reconcile.
  function currentDefinition(): ScanGroup {
    if (tab === 'builder' && state) return rowsToAst(state)
    try {
      return JSON.parse(json) as ScanGroup
    } catch {
      throw new Error('Invalid JSON in the editor — fix it before running')
    }
  }

  function setDefinition(def: ScanGroup) {
    const rows = astToRows(def)
    setState(rows)
    setJson(JSON.stringify(def, null, 2))
    setTab(rows ? 'builder' : 'json')
  }

  async function generate() {
    setNlBusy(true); setError(null); setExplanation(null)
    try {
      const { definition, explanation } = await api.nlScanner(nlPrompt)
      setDefinition(definition)
      setExplanation(explanation)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setNlBusy(false)
    }
  }

  async function runPreview() {
    setBusy(true); setError(null)
    try {
      setPreview(await api.previewScanner(currentDefinition()))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    setBusy(true); setError(null)
    try {
      const body = { name, description, definition: currentDefinition() }
      if (initial) await api.updateScanner(initial.id, body)
      else await api.createScanner(body)
      navigate({ to: '/scanners' })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const updateRow = (gi: number, ri: number, row: Row) => {
    const next = structuredClone(state!)
    next.groups[gi].rows[ri] = row
    setState(next)
  }

  return (
    <div className="space-y-6">
      {/* NL box */}
      <div className="space-y-2 rounded-md border p-4">
        <Label htmlFor="nl">Describe your scan</Label>
        <div className="flex gap-2">
          <Textarea id="nl" value={nlPrompt} onChange={(e) => setNlPrompt(e.target.value)}
            placeholder="e.g. 20 EMA crosses above 50 EMA, RSI above 60, volume twice the 20-day average"
            className="min-h-16" />
          <Button onClick={generate} disabled={nlBusy || !nlPrompt.trim()}>
            {nlBusy ? 'Generating…' : 'Generate'}
          </Button>
        </div>
        {explanation && <p className="text-sm text-muted-foreground">
          Generated: {explanation} Review the conditions below before running.</p>}
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <div><Label htmlFor="name">Name</Label>
          <Input id="name" value={name} onChange={(e) => setName(e.target.value)} /></div>
        <div><Label htmlFor="desc">Description</Label>
          <Input id="desc" value={description} onChange={(e) => setDescription(e.target.value)} /></div>
      </div>

      <Tabs value={tab} onValueChange={(next) => {
        if (next === 'builder' && tab === 'json') {
          try {
            setDefinition(JSON.parse(json))
          } catch {
            setError('Invalid JSON — fix it before switching to the builder tab.')
            return
          }
        }
        if (next === 'json' && tab === 'builder' && state) {
          setJson(JSON.stringify(rowsToAst(state), null, 2))
        }
        setTab(next)
      }}>
        <TabsList>
          <TabsTrigger value="builder" disabled={!state}>Builder</TabsTrigger>
          <TabsTrigger value="json">JSON</TabsTrigger>
        </TabsList>
        <TabsContent value="builder" className="space-y-4">
          {state?.groups.map((g, gi) => (
            <div key={gi} className="space-y-2 rounded-md border p-3">
              <div className="flex items-center gap-2">
                <Select value={g.logic} onValueChange={(logic) => {
                  const next = structuredClone(state)
                  next.groups[gi].logic = logic as 'AND' | 'OR'
                  setState(next)
                }}>
                  <SelectTrigger className="w-20"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="AND">AND</SelectItem>
                    <SelectItem value="OR">OR</SelectItem>
                  </SelectContent>
                </Select>
                <span className="text-sm text-muted-foreground">group {gi + 1}</span>
                {state.groups.length > 1 && (
                  <Button size="sm" variant="ghost" onClick={() => {
                    const next = structuredClone(state)
                    next.groups.splice(gi, 1)
                    setState(next)
                  }}>Remove group</Button>
                )}
              </div>
              {g.rows.map((r, ri) => (
                <div key={ri} className="flex flex-wrap items-center gap-2">
                  <Select value={r.timeframe} onValueChange={(timeframe) =>
                    updateRow(gi, ri, { ...r, timeframe: timeframe as Row['timeframe'] })}>
                    <SelectTrigger className="w-20"><SelectValue /></SelectTrigger>
                    <SelectContent>{TIMEFRAME_OPTIONS.map((t) =>
                      <SelectItem key={t} value={t}>{t}</SelectItem>)}</SelectContent>
                  </Select>
                  <OperandEditor value={r.left} onChange={(left) => updateRow(gi, ri, { ...r, left })} />
                  <Select value={r.op} onValueChange={(op) => updateRow(gi, ri, { ...r, op })}>
                    <SelectTrigger className="w-36"><SelectValue /></SelectTrigger>
                    <SelectContent>{OP_OPTIONS.map((o) =>
                      <SelectItem key={o} value={o}>{o.replace('_', ' ')}</SelectItem>)}</SelectContent>
                  </Select>
                  <OperandEditor allowMult value={r.right}
                    onChange={(right) => updateRow(gi, ri, { ...r, right })} />
                  <Button size="sm" variant="ghost" onClick={() => {
                    const next = structuredClone(state)
                    next.groups[gi].rows.splice(ri, 1)
                    setState(next)
                  }}>✕</Button>
                </div>
              ))}
              <Button size="sm" variant="outline" onClick={() => {
                const next = structuredClone(state)
                next.groups[gi].rows.push(emptyRow())
                setState(next)
              }}>Add condition</Button>
            </div>
          ))}
          <Button variant="outline" onClick={() => {
            const next = structuredClone(state!)
            next.groups.push({ logic: 'AND', rows: [emptyRow()] })
            setState(next)
          }}>Add group</Button>
        </TabsContent>
        <TabsContent value="json">
          <Textarea value={json} onChange={(e) => setJson(e.target.value)}
            className="min-h-64 font-mono text-xs" />
        </TabsContent>
      </Tabs>

      <div className="flex gap-2">
        <Button variant="outline" onClick={runPreview} disabled={busy}>
          {busy ? 'Scanning…' : 'Preview results'}
        </Button>
        <Button onClick={save} disabled={busy || !name.trim()}>
          {initial ? 'Save changes' : 'Create scanner'}
        </Button>
      </div>
      {error && <p className="text-sm text-red-500">{error}</p>}
      {preview && <ResultsTable result={preview} />}
    </div>
  )
}
