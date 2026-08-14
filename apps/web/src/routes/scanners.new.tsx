import { createFileRoute } from '@tanstack/react-router'
import { ScannerBuilder } from '#/components/scanner/ScannerBuilder'
import { Topbar } from '#/components/shared/Topbar'

export const Route = createFileRoute('/scanners/new')({
  component: () => (
    <div className="min-h-screen">
      <Topbar />
      <main className="mx-auto w-full max-w-[1800px] space-y-4 px-6 py-4">
        <h1 className="text-2xl font-bold">New scanner</h1>
        <ScannerBuilder initial={null} />
      </main>
    </div>
  ),
})
