import { useEffect, useState } from 'react'
import { AlertTriangle, Pencil, RefreshCw } from 'lucide-react'

import { errorHint, type ApiError } from '../../api/errors'
import { budgetReasons, describeChatError, errorAttempts, validationMessages } from '../../lib/chat'
import { CredentialPrompt } from '../auth/CredentialPrompt'
import { Button } from '../ui/Button'

function useSecondsLeft(until: number | null): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (until === null || until <= Date.now()) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [until, now])
  return until === null ? 0 : Math.max(0, Math.ceil((until - now) / 1000))
}

/**
 * A failed chat turn. Nothing is resent automatically: "Retry" and "Edit prompt" are explicit
 * user actions, and Retry stays disabled until a 429 Retry-After window has passed.
 */
export function ChatErrorCard({ error, receivedAt, onRetry, onEdit, retryDisabled }: {
  error: ApiError
  receivedAt: string | null
  onRetry: () => void
  onEdit: () => void
  /** e.g. another request is in flight */
  retryDisabled: boolean
}) {
  const view = describeChatError(error)
  const retryUntil = error.retryAfterSeconds !== null && receivedAt ? Date.parse(receivedAt) + error.retryAfterSeconds * 1000 : null
  const secondsLeft = useSecondsLeft(retryUntil)
  const [credentialSaved, setCredentialSaved] = useState(false)
  const attempts = errorAttempts(error.details)
  const reasons = budgetReasons(error.details)
  const fields = validationMessages(error.details)
  // The card already shows the Retry-After countdown and its own explanation for these.
  const hint = error.kind === 'rate_limited' || error.kind === 'unauthorized' || error.code === 'rate_limiter_unavailable'
    ? null : errorHint(error)

  return (
    <div role="alert" className="space-y-3 rounded-md border border-red-200 bg-red-50/70 p-3" data-testid="chat-error">
      <div className="flex gap-2">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" aria-hidden="true" />
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-semibold text-slate-900">{view.title}</p>
          <p className="text-sm text-slate-700">{error.message}</p>
          {view.explanation && <p className="text-xs text-slate-600">{view.explanation}</p>}
          {hint && <p className="text-xs text-slate-500">{hint}</p>}
          {error.retryAfterSeconds !== null && (
            <p className="text-xs font-medium text-slate-700" data-testid="retry-after">
              Retry-After: {error.retryAfterSeconds}s{secondsLeft > 0 ? ` (${secondsLeft}s left)` : ' (elapsed)'}
            </p>
          )}
          {reasons.length > 0 && <p className="text-xs text-slate-600">Budget reasons: <span className="font-mono">{reasons.join(', ')}</span></p>}
          {fields.length > 0 && (
            <ul className="list-inside list-disc text-xs text-slate-600" aria-label="Validation errors">
              {fields.map((f, i) => <li key={`${i}-${f}`}>{f}</li>)}
            </ul>
          )}
          {attempts.length > 0 && (
            <ul className="space-y-0.5 text-xs text-slate-600" aria-label="Failed attempts">
              {attempts.map((a, i) => (
                <li key={`${a.model}-${i}`}><span className="font-mono">{a.model}</span>
                  {a.outcome && <> · {a.outcome}</>}{a.reason && <> · <span className="font-mono">{a.reason}</span></>}</li>
              ))}
            </ul>
          )}
          {error.status !== null && <p className="font-mono text-[11px] text-slate-400">HTTP {error.status} · {error.code}</p>}
        </div>
      </div>
      {view.needsCredential && (credentialSaved
        ? <p className="text-xs text-emerald-700">Chat API key saved. Press Retry to send the request again.</p>
        : <CredentialPrompt kind="chat" rejected onSaved={() => setCredentialSaved(true)} />)}
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" onClick={onRetry} disabled={retryDisabled || secondsLeft > 0}
          icon={<RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />}>
          {secondsLeft > 0 ? `Retry in ${secondsLeft}s` : 'Retry'}
        </Button>
        <Button variant="ghost" size="sm" onClick={onEdit} icon={<Pencil className="h-3.5 w-3.5" aria-hidden="true" />}>Edit prompt</Button>
      </div>
    </div>
  )
}
