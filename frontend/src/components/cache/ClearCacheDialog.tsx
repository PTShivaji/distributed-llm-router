import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { AlertTriangle, Trash2 } from 'lucide-react'

import { api } from '../../api/endpoints'
import { normalizeError, type ApiError } from '../../api/errors'
import type { CacheClearResponse } from '../../api/types'
import { clearErrorView } from '../../lib/cache'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'

export type ClearCacheFn = (adminToken: string, resetStats: boolean) => Promise<{ data: CacheClearResponse }>

/**
 * Confirmation for DELETE /api/v1/cache?reset_stats=…
 *
 * The admin token lives ONLY in the password field's DOM value: it is never copied into React
 * state, the credential store, sessionStorage/localStorage, the URL or logs. It is read once on
 * submit, the field is emptied BEFORE the request is sent, and it is emptied again on cancel,
 * close and unmount. A failed request is never retried automatically; the token must be typed again.
 */
export function ClearCacheDialog({ open, onClose, onCleared, clear = api.clearCache }: {
  open: boolean
  onClose: () => void
  onCleared?: (result: CacheClearResponse) => void
  clear?: ClearCacheFn
}) {
  const tokenField = useRef<HTMLInputElement>(null)
  const [hasToken, setHasToken] = useState(false) // whether the field has text; never the text itself
  const [resetStats, setResetStats] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const wipeToken = useCallback(() => {
    if (tokenField.current) tokenField.current.value = ''
    setHasToken(false)
  }, [])

  const close = useCallback(() => {
    wipeToken()
    setConfirmed(false)
    setResetStats(false)
    setError(null)
    onClose()
  }, [onClose, wipeToken])

  // also wipe if the dialog is hidden or unmounted by the parent
  useEffect(() => {
    const field = tokenField
    return () => {
      if (field.current) field.current.value = ''
    }
  }, [open])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const adminToken = tokenField.current?.value.trim() ?? ''
    if (!adminToken || !confirmed || busy) return
    wipeToken() // discarded from the form before the request is sent
    setBusy(true)
    setError(null)
    try {
      const { data } = await clear(adminToken, resetStats)
      onCleared?.(data)
      close()
    } catch (err) {
      setError(normalizeError(err))
    } finally {
      setBusy(false)
    }
  }

  const view = error ? clearErrorView(error) : null
  return (
    <Dialog open={open} onClose={close} title="Clear response cache"
      description="Deletes cached answers from Redis. This cannot be undone.">
      <form onSubmit={submit} className="space-y-4" autoComplete="off" aria-label="Clear response cache">
        <div className="flex gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900" role="note">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
          <p>
            The cache is <strong>shared</strong>: this removes cached responses for every user, client and backend instance
            that uses this Redis cache, not just yours. Until it refills, repeated prompts call the providers again
            (and, with real providers, cost money again).
          </p>
        </div>
        <div>
          <label htmlFor="admin-token" className="block text-xs font-medium text-slate-700">Admin token</label>
          <input id="admin-token" ref={tokenField} name="admin-token" type="password" autoComplete="off" spellCheck={false}
            disabled={busy} onInput={(e) => setHasToken(e.currentTarget.value.trim() !== '')}
            className="mt-1 block w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-sm shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500" />
          <p className="mt-1 text-xs text-slate-500">ADMIN_API_TOKEN. Sent with this one request as a bearer header, then discarded. Never stored.</p>
        </div>
        <label className="flex items-start gap-2 text-sm text-slate-700">
          <input type="checkbox" className="mt-0.5" checked={resetStats} disabled={busy} onChange={(e) => setResetStats(e.target.checked)} />
          <span>Also reset hit/miss statistics
            <span className="block text-xs text-slate-500">Sends <code className="font-mono">reset_stats=true</code>: the hit, miss and store counters restart from zero. Off by default (counters are kept).</span>
          </span>
        </label>
        <label className="flex items-start gap-2 text-sm text-slate-700">
          <input type="checkbox" className="mt-0.5" checked={confirmed} disabled={busy} onChange={(e) => setConfirmed(e.target.checked)} />
          I understand that all shared cached responses will be deleted
        </label>
        {error && view && (
          <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm" data-testid="clear-error">
            <p className="font-semibold text-slate-900">{view.title}</p>
            <p className="mt-0.5 text-slate-700">{error.message}</p>
            <p className="mt-0.5 text-xs text-slate-600">{view.explanation}</p>
            {error.status !== null && <p className="mt-1 font-mono text-[11px] text-slate-400">HTTP {error.status} · {error.code}</p>}
          </div>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="secondary" onClick={close} disabled={busy}>Cancel</Button>
          <Button type="submit" variant="danger" loading={busy} disabled={!hasToken || !confirmed}
            icon={<Trash2 className="h-4 w-4" aria-hidden="true" />}>
            Clear cache
          </Button>
        </div>
      </form>
    </Dialog>
  )
}
