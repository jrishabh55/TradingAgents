import { useNavigate } from '@tanstack/react-router'
import { useState } from 'react'
import { ConditionRows } from '#/components/scanner/ConditionRows'
import { ResultsTable } from '#/components/scanner/ResultsTable'
import { Button } from '#/components/ui/button'
import { Input } from '#/components/ui/input'
import { Label } from '#/components/ui/label'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '#/components/ui/tabs'
import { Textarea } from '#/components/ui/textarea'
import { api } from '#/lib/api'
import {
  astToRows, emptyRow, rowsToAst, type BuilderState,
} from '#/lib/scanner-rows'
import type { ScanGroup, ScanResult, ScannerSummary } from '#/lib/scanner-types'

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
          {state && <ConditionRows state={state} onChange={setState} />}
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
