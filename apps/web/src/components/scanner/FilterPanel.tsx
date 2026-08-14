import { useState } from 'react'
import { ConditionRows } from '#/components/scanner/ConditionRows'
import { Button } from '#/components/ui/button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '#/components/ui/dialog'
import { Input } from '#/components/ui/input'
import { Label } from '#/components/ui/label'
import { Textarea } from '#/components/ui/textarea'
import { api } from '#/lib/api'
import { astToRows, emptyRow, rowsToAst, type BuilderState } from '#/lib/scanner-rows'
import type { ScanGroup, ScanResult, ScannerSummary } from '#/lib/scanner-types'

/** Where the active filter came from — decides the panel title and which
 *  save path (create vs. update) is offered. Only 'mine' carries an id,
 *  since prebuilts are never updated in place (see filterFromScanner). */
export type FilterOrigin =
  | { kind: 'blank' }
  | { kind: 'nl'; query: string }
  | { kind: 'prebuilt'; name: string }
  | { kind: 'mine'; id: string; name: string; description: string }

export type ActiveFilter = {
  origin: FilterOrigin
  state: BuilderState | null
  json: string
  /** Canonical JSON of the definition as loaded, used only to detect
   *  whether an origin:'prebuilt' filter has been edited (which demotes
   *  its title/save behavior to a temp filter — prebuilts are never
   *  updated in place). */
  originalJson: string
  explanation: string | null
}

export function blankFilter(): ActiveFilter {
  const state: BuilderState = { groups: [{ logic: 'AND', rows: [emptyRow()] }] }
  const def = rowsToAst(state)
  return {
    origin: { kind: 'blank' }, state,
    json: JSON.stringify(def, null, 2), originalJson: JSON.stringify(def), explanation: null,
  }
}

export function filterFromScanner(s: ScannerSummary): ActiveFilter {
  return {
    origin: s.prebuilt
      ? { kind: 'prebuilt', name: s.name }
      : { kind: 'mine', id: s.id, name: s.name, description: s.description },
    state: astToRows(s.definition),
    json: JSON.stringify(s.definition, null, 2),
    // Compact (no pretty-print) so it compares equal to safeJson()'s output
    // below when nothing has actually changed.
    originalJson: JSON.stringify(s.definition),
    explanation: null,
  }
}

export function filterFromNl(query: string, definition: ScanGroup, explanation: string): ActiveFilter {
  return {
    origin: { kind: 'nl', query }, state: astToRows(definition),
    json: JSON.stringify(definition, null, 2), originalJson: JSON.stringify(definition), explanation,
  }
}

function currentDefinition(filter: ActiveFilter): ScanGroup {
  if (filter.state) return rowsToAst(filter.state)
  try {
    return JSON.parse(filter.json) as ScanGroup
  } catch {
    throw new Error('Invalid JSON in the definition — fix it before running')
  }
}

/** Compact-stringifies the current definition for comparison against
 *  `originalJson` (also compact — see filterFromScanner). Returns null
 *  while the JSON fallback textarea holds invalid JSON, which is treated
 *  as "edited". */
function safeJson(filter: ActiveFilter): string | null {
  try {
    return JSON.stringify(currentDefinition(filter))
  } catch {
    return null
  }
}

export function FilterPanel({ filter, onChange, onClear, onSaved, onResult }: {
  filter: ActiveFilter
  onChange: (f: ActiveFilter) => void
  onClear: () => void
  onSaved: (scanner: ScannerSummary, wasUpdate: boolean) => void
  onResult: (result: ScanResult, label: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saveOpen, setSaveOpen] = useState(false)

  const { origin } = filter
  const isPrebuiltEdited = origin.kind === 'prebuilt' &&
    safeJson(filter) !== filter.originalJson
  const title = origin.kind === 'mine' || origin.kind === 'blank'
    ? (origin.kind === 'mine' ? origin.name : 'Unsaved filter')
    : origin.kind === 'prebuilt'
      ? (isPrebuiltEdited ? `Unsaved filter (based on ${origin.name})` : origin.name)
      : 'Unsaved filter' // nl

  async function run() {
    setBusy(true); setError(null)
    try {
      const result = await api.previewScanner(currentDefinition(filter))
      onResult(result, title)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4 rounded-md border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">{title}</h2>
        <div className="flex gap-2">
          <Button variant="outline" onClick={run} disabled={busy}>
            {busy ? 'Scanning…' : 'Run'}
          </Button>
          <Button onClick={() => setSaveOpen(true)} disabled={busy}>Save</Button>
          <Button variant="ghost" onClick={onClear}>Clear</Button>
        </div>
      </div>

      {filter.explanation && (
        <p className="text-sm text-muted-foreground">Generated: {filter.explanation}</p>
      )}

      {filter.state ? (
        <ConditionRows state={filter.state} onChange={(s) =>
          onChange({ ...filter, state: s, json: JSON.stringify(rowsToAst(s), null, 2) })} />
      ) : (
        <div className="space-y-2">
          <p className="text-sm text-muted-foreground">
            This definition uses features the simple editor can&apos;t represent
            (candlestick patterns, deeper nesting). Edit the raw definition below.
          </p>
          <Textarea value={filter.json} onChange={(e) => onChange({ ...filter, json: e.target.value })}
            className="min-h-64 font-mono text-xs" />
        </div>
      )}

      {error && <p className="text-sm text-red-500">{error}</p>}

      <SaveDialog open={saveOpen} onOpenChange={setSaveOpen} filter={filter}
        getDefinition={() => currentDefinition(filter)}
        onSaved={(s, wasUpdate) => { setSaveOpen(false); onSaved(s, wasUpdate) }} />
    </div>
  )
}

function SaveDialog({ open, onOpenChange, filter, getDefinition, onSaved }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  filter: ActiveFilter
  getDefinition: () => ScanGroup
  onSaved: (scanner: ScannerSummary, wasUpdate: boolean) => void
}) {
  const mineOrigin = filter.origin.kind === 'mine' ? filter.origin : null
  const [name, setName] = useState(mineOrigin?.name ?? '')
  const [description, setDescription] = useState(mineOrigin?.description ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Reset the form to match whichever saved scanner (if any) is currently
  // loaded, each time the dialog opens — otherwise a previous save's name
  // would linger into the next one.
  function handleOpenChange(next: boolean) {
    if (next) {
      setName(mineOrigin?.name ?? '')
      setDescription(mineOrigin?.description ?? '')
      setError(null)
    }
    onOpenChange(next)
  }

  const canUpdate = mineOrigin !== null && name.trim() === mineOrigin.name

  async function submit(update: boolean) {
    if (!name.trim()) { setError('Name is required'); return }
    setBusy(true); setError(null)
    try {
      const body = { name: name.trim(), description: description.trim(), definition: getDefinition() }
      const scanner = update && mineOrigin
        ? await api.updateScanner(mineOrigin.id, body)
        : await api.createScanner(body)
      onSaved(scanner, update && !!mineOrigin)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Save filter</DialogTitle>
          <DialogDescription>
            {canUpdate
              ? `Update the existing scanner, or save this as a new one.`
              : `Give this filter a name to save it as a scanner.`}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <Label htmlFor="save-name">Name</Label>
            <Input id="save-name" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          </div>
          <div>
            <Label htmlFor="save-desc">Description</Label>
            <Textarea id="save-desc" value={description}
              onChange={(e) => setDescription(e.target.value)} className="min-h-16" />
          </div>
          {error && <p className="text-sm text-red-500">{error}</p>}
        </div>
        <DialogFooter>
          {canUpdate && (
            <Button variant="outline" disabled={busy} onClick={() => submit(false)}>
              Save as new
            </Button>
          )}
          <Button disabled={busy || !name.trim()} onClick={() => submit(canUpdate)}>
            {busy ? 'Saving…' : canUpdate ? `Update "${mineOrigin!.name}"` : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
