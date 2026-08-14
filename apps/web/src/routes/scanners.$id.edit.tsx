import { createFileRoute } from '@tanstack/react-router'
import { ScannerBuilder } from '#/components/scanner/ScannerBuilder'
import { Topbar } from '#/components/shared/Topbar'
import { api } from '#/lib/api'

export const Route = createFileRoute('/scanners/$id/edit')({
  /* Client-only: same-origin fetch during SSR has no origin (see scanners.index). */
  ssr: false,
  loader: async ({ params }) => {
    const scanners = await api.listScanners()
    const scanner = scanners.find((s) => s.id === params.id)
    if (!scanner || scanner.prebuilt) throw new Error('scanner not found')
    return { scanner }
  },
  component: EditPage,
})

function EditPage() {
  const { scanner } = Route.useLoaderData()
  return (
    <div className="min-h-screen">
      <Topbar />
      <main className="mx-auto max-w-5xl space-y-4 p-4">
        <h1 className="text-2xl font-bold">Edit — {scanner.name}</h1>
        <ScannerBuilder initial={scanner} />
      </main>
    </div>
  )
}
