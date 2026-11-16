import type { ReactNode } from 'react'
import { AlertTriangle, Clock, Inbox, KeyRound, RefreshCw, ServerCrash, ShieldAlert, WifiOff } from 'lucide-react'
import { Link } from 'react-router'

import { errorHint, normalizeError, type ApiError, type ApiErrorKind } from '../../api/errors'
import { Button } from '../ui/Button'
import { Spinner } from '../ui/Spinner'

export function LoadingState({ label = 'Loading…' }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="flex items-center justify-center gap-3 py-12 text-sm text-slate-500">
      <Spinner />
      <span>{label}</span>
    </div>
  )
}

/** Placeholder blocks while content loads (no numbers, no fake values). */
export function SkeletonRows({ rows = 3 }: { rows?: number }) {
  return (
    <div aria-hidden="true" className="space-y-3">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="h-4 animate-pulse rounded bg-slate-100" style={{ width: `${90 - i * 12}%` }} />
      ))}
    </div>
  )
}

export function EmptyState({ title, description, icon, action }: {
  title: string; description?: ReactNode; icon?: ReactNode; action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      <div className="mb-3 rounded-full bg-slate-100 p-3 text-slate-400">{icon ?? <Inbox className="h-6 w-6" aria-hidden="true" />}</div>
      <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
      {description && <div className="mt-1 max-w-md text-sm text-slate-500">{description}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

const ICONS: Partial<Record<ApiErrorKind, ReactNode>> = {
  unauthorized: <KeyRound className="h-6 w-6" aria-hidden="true" />,
  forbidden: <ShieldAlert className="h-6 w-6" aria-hidden="true" />,
  rate_limited: <Clock className="h-6 w-6" aria-hidden="true" />,
  budget_exceeded: <Clock className="h-6 w-6" aria-hidden="true" />,
  network: <WifiOff className="h-6 w-6" aria-hidden="true" />,
  unavailable: <ServerCrash className="h-6 w-6" aria-hidden="true" />,
  server: <ServerCrash className="h-6 w-6" aria-hidden="true" />,
}

const TITLES: Partial<Record<ApiErrorKind, string>> = {
  unauthorized: 'Authentication required',
  forbidden: 'Not allowed',
  not_found: 'Not found',
  validation: 'Invalid request',
  rate_limited: 'Rate limit reached',
  budget_exceeded: 'Spend limit reached',
  unavailable: 'Service unavailable',
  network: 'Backend unreachable',
  timeout: 'Request timed out',
}

/** Renders any error; ApiError gets a kind-specific title, hint and request id. */
export function ErrorState({ error, onRetry, compact = false }: { error: unknown; onRetry?: () => void; compact?: boolean }) {
  const e: ApiError = normalizeError(error)
  const hint = errorHint(e)
  return (
    <div role="alert" className={compact ? 'rounded-md border border-red-200 bg-red-50 p-4' : 'flex flex-col items-center px-6 py-12 text-center'}>
      {!compact && <div className="mb-3 rounded-full bg-red-50 p-3 text-red-500">{ICONS[e.kind] ?? <AlertTriangle className="h-6 w-6" aria-hidden="true" />}</div>}
      <h3 className="text-sm font-semibold text-slate-900">
        {e.code === 'rate_limiter_unavailable' ? 'Backend busy' : (TITLES[e.kind] ?? 'Something went wrong')}
      </h3>
      <p className="mt-1 max-w-lg text-sm text-slate-600">{e.message}</p>
      {hint && <p className="mt-1 max-w-lg text-xs text-slate-500">{hint}</p>}
      {(e.status !== null || e.requestId) && (
        <p className="mt-2 font-mono text-[11px] text-slate-400">
          {e.status !== null && <>HTTP {e.status} · {e.code}</>}
          {e.requestId && <> · request {e.requestId}</>}
        </p>
      )}
      <div className="mt-4 flex gap-2">
        {e.kind === 'unauthorized' && <Link to="/settings" className="text-sm font-medium text-brand-700 hover:underline">Open Settings</Link>}
        {onRetry && e.kind !== 'unauthorized' && (
          <Button variant="secondary" size="sm" icon={<RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />} onClick={onRetry}>
            Retry
          </Button>
        )}
      </div>
    </div>
  )
}
