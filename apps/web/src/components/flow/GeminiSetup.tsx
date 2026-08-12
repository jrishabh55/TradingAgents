import { useState } from 'react'
import { api } from '#/lib/api'
import type { GeminiKeyStatus } from '#/lib/types'

/** Gemini BYOC credential state + setup, shown when the selected provider has
 *  `requires_user_key`. Resolved credential → subtle confirmation; otherwise a
 *  compact card explaining the two paths (Google account / pasted key).
 *  Fetching stays with FlowLanding — this only renders the `status` it is
 *  handed and calls `onChanged` after a save/remove so the parent refetches. */
export function GeminiSetup({
  status,
  onChanged,
}: {
  status: GeminiKeyStatus | null
  onChanged: () => void
}) {
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save() {
    if (!key.trim()) return
    setBusy(true)
    setError(null)
    try {
      await api.saveGeminiKey(key.trim())
      setKey('')
      onChanged()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    setBusy(true)
    setError(null)
    try {
      await api.deleteGeminiKey()
      onChanged()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (!status) {
    return (
      <div style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
        Checking your Gemini credential…
      </div>
    )
  }

  if (status.active_source === 'oauth') {
    return (
      <div style={{ fontSize: 11.5, color: 'var(--ok)' }}>
        Connected via your Google account
      </div>
    )
  }

  if (status.active_source === 'manual') {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          fontSize: 11.5,
        }}
      >
        <span style={{ color: 'var(--ok)' }}>
          Gemini key saved ····{status.last4}
        </span>
        <button className="es-btn ghost sm" onClick={() => void remove()} disabled={busy}>
          Remove
        </button>
        {error && <span style={{ color: 'var(--err)' }}>{error}</span>}
      </div>
    )
  }

  /* No usable credential yet — the run button is blocked until this resolves. */
  return (
    <div
      style={{
        width: '100%',
        maxWidth: 560,
        textAlign: 'left',
        background: 'var(--bg-1)',
        border: '1px solid rgba(251,146,60,0.35)',
        borderRadius: 12,
        padding: '14px 16px',
        display: 'grid',
        gap: 10,
        fontSize: 12,
      }}
    >
      <div style={{ fontSize: 12.5, fontWeight: 600, color: '#fb923c' }}>
        Gemini needs your own API key
      </div>
      <div style={{ color: 'var(--text-3)' }}>
        Gemini runs on your own API key — never on a shared server key.
      </div>
      {status.oauth_available && status.oauth_error && (
        <div style={{ fontSize: 11.5, color: 'var(--err)' }}>
          Your Google sign-in couldn't be used: {status.oauth_error}
        </div>
      )}
      <div style={{ fontSize: 11.5, color: 'var(--text-3)' }}>
        Get a free key from{' '}
        <a
          href="https://aistudio.google.com/apikey"
          target="_blank"
          rel="noreferrer"
          style={{ color: 'var(--accent-hi)' }}
        >
          Google AI Studio ↗
        </a>{' '}
        and paste it below:
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          className="fld-input"
          style={{ flex: 1 }}
          type="password"
          placeholder="AIza… Gemini API key"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          spellCheck={false}
        />
        <button
          className="es-btn sm"
          onClick={() => void save()}
          disabled={busy || !key.trim()}
        >
          {busy ? 'Verifying…' : 'Save key'}
        </button>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
        The key is verified against the Gemini API, then stored encrypted. It
        is never shown again — only its last 4 characters.
      </div>
      {error && <div style={{ fontSize: 11.5, color: 'var(--err)' }}>{error}</div>}
    </div>
  )
}
