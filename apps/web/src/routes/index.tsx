import { createFileRoute, redirect } from '@tanstack/react-router'

/* The scanner workbench is the default page; the analysis form lives at /analyse. */
export const Route = createFileRoute('/')({
  beforeLoad: () => {
    throw redirect({ to: '/scanners' })
  },
})
