import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/* webapp1's `analyst.report` events typically carry the full report in one
   shot (LangGraph emits the whole node output when the node finishes — agents
   don't stream tokens). So "streaming" here is a perceived-streaming
   typewriter: when StreamingMarkdown mounts with `animate=true` we start the
   displayed prefix at zero length and reveal up to the full content via rAF.
   Subsequent prop growth (rare but possible if the backend ever moves to true
   token streaming) animates only the delta. */

const REVEAL_RATE = 1500 // chars/sec — feels like fast typing
const MIN_DURATION_MS = 280
const MAX_DURATION_MS = 1400

export interface StreamingMarkdownProps {
  content: string
  /* When true, animate. When false, snap straight to `content` — used when
     the user clicks an already-completed tab to re-read it. */
  animate?: boolean
  /* Whether the source is still live. Drives the trailing cursor. */
  live?: boolean
}

export function StreamingMarkdown({
  content,
  animate = true,
  live = false,
}: StreamingMarkdownProps) {
  /* Critical: when animating, start at empty so the first useEffect run sees
     a non-zero delta to reveal. With `useState(content)` we'd start at the
     destination and animation would never fire. */
  const [displayed, setDisplayed] = useState<string>(animate ? '' : content)
  const displayedRef = useRef<string>(displayed)
  displayedRef.current = displayed

  useEffect(() => {
    if (!animate) {
      setDisplayed(content)
      return
    }
    /* Shrink (e.g. parent reset): snap. */
    if (content.length <= displayedRef.current.length) {
      setDisplayed(content)
      return
    }

    let raf = 0
    const startedFrom = displayedRef.current.length
    const startTime = performance.now()
    const delta = content.length - startedFrom
    const duration = Math.min(
      MAX_DURATION_MS,
      Math.max(MIN_DURATION_MS, (delta * 1000) / REVEAL_RATE),
    )

    const step = (now: number) => {
      const elapsed = now - startTime
      const progress = Math.min(1, elapsed / duration)
      /* easeOutCubic — fast start, soft landing so the last few characters
         don't pop in abruptly. */
      const eased = 1 - Math.pow(1 - progress, 3)
      const targetLen = startedFrom + Math.round(delta * eased)
      setDisplayed(content.slice(0, targetLen))
      if (progress < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [content, animate])

  const isAnimating = displayed.length < content.length
  const showCursor = isAnimating || live

  return (
    <>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{displayed}</ReactMarkdown>
      {showCursor && <span className="streaming-cursor" aria-hidden="true" />}
    </>
  )
}
