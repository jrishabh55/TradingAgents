/** Case-insensitive subsequence fuzzy match: every character of `query`
 *  appears in `text`, in order, not necessarily contiguous. An empty (or
 *  whitespace-only) query matches everything — used by the scanner
 *  workbench's inline Command box, which drives its own filtering
 *  (`shouldFilter={false}`) so that the "generate" / "new blank filter"
 *  footer items can stay visible regardless of what cmdk's default
 *  matcher would say about them. */
export function fuzzyMatch(query: string, text: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  const t = text.toLowerCase()
  let ti = 0
  for (const ch of q) {
    const found = t.indexOf(ch, ti)
    if (found === -1) return false
    ti = found + 1
  }
  return true
}
