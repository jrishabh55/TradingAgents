import { Pencil, X } from 'lucide-react'
import { useState } from 'react'
import { ConditionRows } from '#/components/scanner/ConditionRows'
import { Button } from '#/components/ui/button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '#/components/ui/dialog'
import { Input } from '#/components/ui/input'
import { Label } from '#/components/ui/label'
import { Switch } from '#/components/ui/switch'
import { Textarea } from '#/components/ui/textarea'
import { api } from '#/lib/api'
import {
  astToRows, canonicalScanJson, describeRow, emptyRow, rowsToAst, withLiquidityFloor,
  type BuilderState,
} from '#/lib/scanner-rows'
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
  /** Whether the panel is showing its slim applied-state summary bar
   *  instead of the full editor. Set true by a successful Run (see
   *  FilterPanel::run), by selecting a scanner from the list (see
   *  filterFromScanner's `collapsed` option and ScannersPage::selectScanner
   *  — collapsed from the moment the ActiveFilter is created, so there's no
   *  expanded-then-collapsed flash while its auto-preview is in flight) —
   *  never by a manual "collapse without running" action, since that would
   *  let the chips (reflecting live edits) drift out of sync with a stale
   *  matchCount badge. False whenever a fresh, not-yet-run, not-yet-picked
   *  filter is loaded (blank, NL-generated), or the user hits Edit. */
  collapsed: boolean
  /** Match count from the most recent successful run, shown as a badge on
   *  the collapsed summary bar. Null until a run (or an initial scanner
   *  preview) has completed. */
  matchCount: number | null
  /** "Liquid only" toggle — 20d avg volume >= 100k. Applied at run time via
   *  withLiquidityFloor (see FilterPanel::run), never merged into `state`/
   *  `json`, never saved, and never part of dirty-detection. Defaults to
   *  true for every freshly-created ActiveFilter (blank, NL-generated, or
   *  loaded from a saved scanner) — it isn't part of the saved scanner
   *  schema, so there's nothing to restore it from. */
  liquidOnly: boolean
}

export function blankFilter(): ActiveFilter {
  const state: BuilderState = { groups: [{ logic: 'AND', rows: [emptyRow()] }] }
  const def = rowsToAst(state)
  return {
    origin: { kind: 'blank' }, state,
    json: JSON.stringify(def, null, 2), originalJson: canonicalScanJson(def), explanation: null,
    collapsed: false, matchCount: null, liquidOnly: true,
  }
}

/** @param opts.collapsed - Pass `true` when loading a scanner the user
 *  picked from a list (see ScannersPage::selectScanner) so the panel renders
 *  its slim summary bar (chips + name) immediately, with no expanded flash
 *  before the auto-preview lands — the badge just fills in matchCount once
 *  that resolves. Defaults to `false` for callers reloading a filter after
 *  a save (see ScannersPage::handleSaved), which stays in the full editor
 *  since the user was mid-edit. */
export function filterFromScanner(s: ScannerSummary, opts?: { collapsed?: boolean }): ActiveFilter {
  return {
    origin: s.prebuilt
      ? { kind: 'prebuilt', name: s.name }
      : { kind: 'mine', id: s.id, name: s.name, description: s.description },
    state: astToRows(s.definition),
    json: JSON.stringify(s.definition, null, 2),
    // Normalized through the same rows round-trip that the "current" side
    // goes through (see canonicalScanJson) — comparing raw API JSON against
    // a round-tripped reconstruction would flag unedited fn operands (e.g.
    // `{ fn: 'MACD' }`, which gains an implicit `of: 'close'`) as dirty.
    originalJson: canonicalScanJson(s.definition),
    explanation: null,
    collapsed: opts?.collapsed ?? false, matchCount: null, liquidOnly: true,
  }
}

export function filterFromNl(query: string, definition: ScanGroup, explanation: string): ActiveFilter {
  return {
    origin: { kind: 'nl', query }, state: astToRows(definition),
    json: JSON.stringify(definition, null, 2),
    originalJson: canonicalScanJson(definition),
    explanation,
    collapsed: false, matchCount: null, liquidOnly: true,
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

/** Canonical JSON of the current definition, for comparison against
 *  `originalJson` (see canonicalScanJson — both sides go through the same
 *  normalization). Returns null while the JSON fallback textarea holds
 *  invalid JSON, which is treated as "edited". */
function safeCanonicalJson(filter: ActiveFilter): string | null {
  try {
    return canonicalScanJson(currentDefinition(filter))
  } catch {
    return null
  }
}

export function FilterPanel({ filter, onChange, onClear, onSaved, onResult, previewing = false }: {
  filter: ActiveFilter
  onChange: (f: ActiveFilter) => void
  onClear: () => void
  onSaved: (scanner: ScannerSummary, wasUpdate: boolean) => void
  onResult: (result: ScanResult, label: string) => void
  /** True while the page-level auto-preview (scanner selection) is in flight —
   *  the collapsed bar shows a scanning pill instead of a stale/absent count. */
  previewing?: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saveOpen, setSaveOpen] = useState(false)

  const { origin } = filter
  const isPrebuiltEdited = origin.kind === 'prebuilt' &&
    safeCanonicalJson(filter) !== filter.originalJson
  const title = origin.kind === 'mine' || origin.kind === 'blank'
    ? (origin.kind === 'mine' ? origin.name : 'Unsaved filter')
    : origin.kind === 'prebuilt'
      ? (isPrebuiltEdited ? `Unsaved filter (based on ${origin.name})` : origin.name)
      : 'Unsaved filter' // nl

  async function run() {
    setBusy(true); setError(null)
    try {
      const result = await api.previewScanner(withLiquidityFloor(currentDefinition(filter), filter.liquidOnly))
      onResult(result, title)
      // Auto-collapse only on a *successful* run — editing conditions or a
      // failed run leaves the editor expanded (see module doc comment on
      // ActiveFilter.collapsed).
      onChange({ ...filter, collapsed: true, matchCount: result.matches.length })
    } catch (e) {
      let message = e instanceof Error ? e.message : String(e)
      // The 422 node-count error only counts visible conditions — a user
      // who never touched the (invisible) liquidity floor would otherwise
      // have no idea it's the thing pushing them over the limit.
      if (filter.liquidOnly && message.includes('too many conditions')) {
        message += " — the 'Liquid only' toggle adds one hidden condition; turn it off or remove a condition."
      }
      setError(message)
    } finally {
      setBusy(false)
    }
  }

  const rowChips = filter.state
    ? filter.state.groups.flatMap((g) => g.rows).map(describeRow)
    : []
  const chips = rowChips.length ? rowChips : ['advanced filter']

  return (
    <div className="space-y-2">
      {filter.collapsed ? (
        <div className="flex min-h-10 items-center gap-3 rounded-lg border border-[var(--line-1)] bg-card px-3 py-1.5 shadow-[var(--shadow-1)]">
          <div className="flex shrink-0 items-center gap-2">
            <span className="size-2 shrink-0 rounded-full bg-primary" />
            <span className="text-sm font-semibold">{title}</span>
          </div>
          <div className="h-4 w-px shrink-0 bg-border" />
          <div className="flex flex-1 items-center gap-1.5 overflow-x-auto">
            {chips.map((c, i) => (
              <span key={i} className="es-chip">{c}</span>
            ))}
            {previewing ? (
              <span className="es-pill run ml-1 shrink-0">
                <span className="es-dot pulse" />
                Scanning…
              </span>
            ) : filter.matchCount != null && (
              <span className="es-pill accent ml-1 shrink-0">{filter.matchCount} matches</span>
            )}
            {filter.liquidOnly && (
              <span className="es-chip shrink-0 text-muted-foreground">liquid</span>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-1">
            <Button size="sm" variant="ghost" className="h-6 px-2 text-xs text-primary"
              onClick={() => onChange({ ...filter, collapsed: false })}>
              <Pencil className="size-3" /> Edit
            </Button>
            <Button size="icon-xs" variant="ghost" aria-label="Clear filter"
              className="text-muted-foreground hover:text-destructive" onClick={onClear}>
              <X className="size-3.5" />
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-2 rounded-[var(--r-lg)] border border-[var(--line-1)] bg-card p-2 shadow-[var(--shadow-1)]">
          <div className="flex flex-wrap items-center justify-between gap-2 px-1">
            <h2 className="flex items-center gap-1.5 text-sm font-semibold">
              <span className="es-team-label opacity-60">Filter</span>
              {title}
            </h2>
            <div className="flex gap-1.5">
              <Button size="sm" variant="outline" onClick={run} disabled={busy}>
                {busy ? 'Scanning…' : 'Run'}
              </Button>
              <Button size="sm" onClick={() => setSaveOpen(true)} disabled={busy}>Save</Button>
              <Button size="sm" variant="ghost" onClick={onClear}>Clear</Button>
            </div>
          </div>

          <div className="flex items-center gap-2 px-1">
            <Switch id="liquid-only" size="sm" checked={filter.liquidOnly} disabled={busy}
              onCheckedChange={(v) => onChange({ ...filter, liquidOnly: v })} />
            <Label htmlFor="liquid-only" className="text-xs font-medium">Liquid only</Label>
            <span className="text-xs text-muted-foreground">20d avg volume &ge; 100k</span>
          </div>

          {filter.explanation && (
            <p className="px-1 text-xs text-muted-foreground">Generated: {filter.explanation}</p>
          )}

          {filter.state ? (
            <ConditionRows state={filter.state} onChange={(s) =>
              onChange({ ...filter, state: s, json: JSON.stringify(rowsToAst(s), null, 2) })} />
          ) : (
            <div className="space-y-2 px-1">
              <p className="text-xs text-muted-foreground">
                This definition uses features the simple editor can&apos;t represent
                (candlestick patterns, deeper nesting). Edit the raw definition below.
              </p>
              <Textarea value={filter.json} onChange={(e) => onChange({ ...filter, json: e.target.value })}
                className="min-h-64 font-mono text-xs" />
            </div>
          )}

          {error && <p className="px-1 text-sm text-[var(--err)]">{error}</p>}
        </div>
      )}

      {/* Radix's Dialog only calls onOpenChange for its own internal
          close interactions (Escape, overlay click, the X button) — it
          never fires when a parent flips the controlled `open` prop from
          false to true, so a "reset on open" effect/handler inside
          SaveDialog would never actually run. Keying by filter identity +
          open state forces a full remount (fresh useState defaults) each
          time the dialog opens against a (possibly different) filter. */}
      <SaveDialog key={`${origin.kind === 'mine' ? origin.id : 'temp'}-${saveOpen}`}
        open={saveOpen} onOpenChange={setSaveOpen} filter={filter}
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
  // The `key` on this component (see FilterPanel) forces a remount — and
  // therefore fresh initial state here — every time the dialog opens or
  // the underlying filter's saved-scanner identity changes.
  const mineOrigin = filter.origin.kind === 'mine' ? filter.origin : null
  const [name, setName] = useState(mineOrigin?.name ?? '')
  const [description, setDescription] = useState(mineOrigin?.description ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
    <Dialog open={open} onOpenChange={onOpenChange}>
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
