import { createFileRoute, Link } from '@tanstack/react-router'
import { useState } from 'react'
import { ResultsTable } from '#/components/scanner/ResultsTable'
import { Topbar } from '#/components/shared/Topbar'
import { Badge } from '#/components/ui/badge'
import { Button } from '#/components/ui/button'
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from '#/components/ui/card'
import { api, getAuthToken } from '#/lib/api'
import type { ScanResult, ScannerSummary } from '#/lib/scanner-types'

export const Route = createFileRoute('/scanners/')({
  loader: async () => {
    await getAuthToken()
    return { scanners: await api.listScanners() }
  },
  component: ScannersPage,
})

function ScannersPage() {
  const { scanners } = Route.useLoaderData()
  const [items, setItems] = useState<ScannerSummary[]>(scanners)
  const [running, setRunning] = useState<string | null>(null)
  const [result, setResult] = useState<{ name: string; data: ScanResult } | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function run(s: ScannerSummary) {
    setRunning(s.id); setError(null)
    try {
      setResult({ name: s.name, data: await api.runScanner(s.id) })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRunning(null)
    }
  }

  async function remove(s: ScannerSummary) {
    await api.deleteScanner(s.id)
    setItems(items.filter((i) => i.id !== s.id))
  }

  const prebuilt = items.filter((s) => s.prebuilt)
  const mine = items.filter((s) => !s.prebuilt)

  const section = (title: string, list: ScannerSummary[], editable: boolean) => (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold">{title}</h2>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {list.map((s) => (
          <Card key={s.id}>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                {s.name}
                {s.prebuilt && <Badge variant="secondary">prebuilt</Badge>}
              </CardTitle>
              <CardDescription>{s.description}</CardDescription>
            </CardHeader>
            <CardContent className="flex gap-2">
              <Button size="sm" disabled={running === s.id} onClick={() => run(s)}>
                {running === s.id ? 'Scanning…' : 'Run'}
              </Button>
              {editable && (
                <>
                  <Button size="sm" variant="outline" asChild>
                    <Link to="/scanners/$id/edit" params={{ id: s.id }}>
                      Edit
                    </Link>
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => remove(s)}>Delete</Button>
                </>
              )}
            </CardContent>
          </Card>
        ))}
        {!list.length && <p className="text-sm text-muted-foreground">None yet.</p>}
      </div>
    </section>
  )

  return (
    <div className="min-h-screen">
      <Topbar state="idle" />
      <main className="mx-auto max-w-6xl space-y-8 p-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold">Scanners</h1>
          <Button asChild><Link to="/scanners/new">New scanner</Link></Button>
        </div>
        {section('Prebuilt', prebuilt, false)}
        {section('My scanners', mine, true)}
        {error && <p className="text-sm text-red-500">{error}</p>}
        {result && (
          <section className="space-y-2">
            <h2 className="text-lg font-semibold">Results — {result.name}</h2>
            <ResultsTable result={result.data} />
          </section>
        )}
      </main>
    </div>
  )
}
