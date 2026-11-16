import { AlertTriangle } from 'lucide-react'

import type { ApiError } from '../../api/errors'
import { formatClock } from '../../lib/format'
import { SectionError } from '../feedback/SectionError'

const TITLES: Partial<Record<ApiError['kind'], string>> = {
  unauthorized: 'the observability token was rejected',
  forbidden: 'access was refused (HTTP 403)',
  rate_limited: 'the rate limit was reached',
  unavailable: 'the service was unavailable',
  network: 'the backend was unreachable',
  timeout: 'the request timed out',
  not_found: 'the endpoint was not found',
}

/** A failed refresh keeps earlier data visible, and says so. Never retries on its own. */
export function StaleNotice({ error, at, what }: { error: ApiError; at: number | null; what: string }) {
  const reason = error.code === 'malformed_response' ? 'the response was malformed' : TITLES[error.kind] ?? 'the request failed'
  return (
    <p className="flex gap-2 rounded-md border border-amber-200 bg-amber-50 p-2.5 text-xs text-amber-900" role="status" data-testid="stale-notice">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" aria-hidden="true" />
      <span>
        Refresh failed: {reason}{error.retryAfterSeconds !== null ? ` (retry after ${error.retryAfterSeconds}s)` : ''}.
        Showing earlier {what}{at ? ` from ${formatClock(new Date(at))}` : ''}; it may be out of date. Nothing is retried automatically.
        {error.status !== null && <span className="ml-1 font-mono text-[11px] text-amber-700">HTTP {error.status} · {error.code}</span>}
      </span>
    </p>
  )
}

export function MalformedState({ error }: { error: ApiError }) {
  return (
    <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-4 text-sm" data-testid="malformed">
      <p className="font-semibold text-slate-900">Unexpected response</p>
      <p className="mt-0.5 text-slate-700">{error.message}</p>
      <p className="mt-0.5 text-xs text-slate-600">Nothing was interpreted from it. Check that the frontend points at this router&apos;s backend, then press Refresh.</p>
    </div>
  )
}

/**
 * Failure of one section's request. With earlier data: a stale notice (plus the token prompt on 401).
 * Without: malformed / credential prompt / kind-specific error. `prompt=false` defers the token prompt
 * to another section so the page never shows two prompts for the same credential.
 */
export function SectionFailure({ error, hasData, at, what, onRetry, prompt = true,
  elsewhere = 'Enter it in the health section above; both sections reload with it.' }: {
  error: ApiError; hasData: boolean; at: number | null; what: string; onRetry: () => void; prompt?: boolean
  /** Where the page's single token prompt is (shown when `prompt` is false). */
  elsewhere?: string
}) {
  const unauthorized = error.kind === 'unauthorized'
  if (hasData) {
    return (
      <div className="space-y-2">
        <StaleNotice error={error} at={at} what={what} />
        {unauthorized && prompt && <SectionError error={error} credential="observability" onRetry={onRetry} />}
      </div>
    )
  }
  if (error.code === 'malformed_response') return <MalformedState error={error} />
  if (unauthorized && !prompt) {
    return (
      <p className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900" role="status" data-testid="token-elsewhere">
        This section also needs the observability token. {elsewhere}
      </p>
    )
  }
  return <SectionError error={error} credential="observability" onRetry={onRetry} />
}
