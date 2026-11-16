import type { ReactNode } from 'react'
import { ArrowRight, Timer } from 'lucide-react'

import type { ApiError } from '../../api/errors'
import type { RouterCounters } from '../../lib/prometheus'
import { formatInteger } from '../../lib/format'
import { SectionError } from '../feedback/SectionError'
import { EmptyState, SkeletonRows } from '../feedback/States'
import { Badge } from '../ui/Badge'
import { Card, CardBody, CardHeader } from '../ui/Card'

function formatUptime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const d = Math.floor(s / 86_400)
  const h = Math.floor((s % 86_400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m ${s % 60}s`
}

function CountList({ title, empty, children, count }: { title: string; empty: string; children: ReactNode; count: number }) {
  return (
    <div className="min-w-0 space-y-1.5">
      <p className="text-xs font-semibold text-slate-700">{title}</p>
      {count === 0 ? <p className="text-xs text-slate-400">{empty}</p> : children}
    </div>
  )
}

/**
 * Router counters from GET /metrics. They accumulate since the backend PROCESS started, per
 * instance, and are NOT filtered by the selected date range.
 */
export function ProcessCountersCard({ counters, error, loading, uptimeSeconds, onRetry }: {
  counters: RouterCounters | null
  error: ApiError | null
  loading: boolean
  uptimeSeconds: number | null
  onRetry: () => void
}) {
  return (
    <Card data-testid="process-counters">
      <CardHeader title="Retries and fallbacks since process start"
        description="Prometheus counters from GET /metrics. Cumulative for this backend instance since it started; not filtered by the date range above and reset on restart."
        actions={<span title="Uptime reported by GET /health at its last check"><Badge tone="neutral" icon={<Timer className="h-3 w-3" aria-hidden="true" />}>
          {uptimeSeconds === null ? 'Since process start' : `Process up ${formatUptime(uptimeSeconds)}`}
        </Badge></span>} />
      <CardBody>
        {error ? (
          error.kind === 'not_found'
            ? <EmptyState title="Metrics are disabled on this backend" description="GET /metrics returned 404 (METRICS_ENABLED=false). Retry and fallback counters are unavailable; the date-ranged analytics above are unaffected." />
            : <SectionError error={error} credential="observability" onRetry={onRetry} />
        ) : !counters ? (
          loading ? <SkeletonRows rows={4} /> : null
        ) : !counters.recognised ? (
          <EmptyState title="No router metrics found" description="The /metrics response did not contain any llm_router_* series." />
        ) : (
          <div className="space-y-4">
            <dl className="grid grid-cols-3 gap-3 text-center">
              {([['Chat requests', counters.chatRequests], ['Retries', counters.retriesTotal], ['Fallbacks', counters.fallbacksTotal]] as const).map(([label, value]) => (
                <div key={label} className="rounded-md bg-slate-50 px-2 py-2.5">
                  <dt className="text-[11px] text-slate-500">{label}</dt>
                  <dd className="text-lg font-semibold tabular-nums text-slate-900" data-testid={`counter-${label}`}>{formatInteger(value)}</dd>
                </div>
              ))}
            </dl>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <CountList title="Retries by model" empty="No retries recorded." count={counters.retries.length}>
                <ul className="space-y-1 text-xs" aria-label="Retries by model">
                  {counters.retries.map((r) => (
                    <li key={r.model} className="flex justify-between gap-2"><span className="truncate font-mono">{r.model}</span><span className="tabular-nums">{formatInteger(r.count)}</span></li>
                  ))}
                </ul>
              </CountList>
              <CountList title="Fallbacks (from → to)" empty="No fallbacks recorded." count={counters.fallbacks.length}>
                <ul className="space-y-1 text-xs" aria-label="Fallbacks">
                  {counters.fallbacks.map((f) => (
                    <li key={`${f.from}>${f.to}`} className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-center gap-1 font-mono"><span className="truncate">{f.from}</span>
                        <ArrowRight className="h-3 w-3 shrink-0 text-slate-400" aria-label="to" /><span className="truncate">{f.to}</span></span>
                      <span className="tabular-nums">{formatInteger(f.count)}</span>
                    </li>
                  ))}
                </ul>
              </CountList>
              <CountList title="Failed attempts by error kind" empty="No failed provider attempts." count={counters.errorKinds.length}>
                <ul className="space-y-1 text-xs" aria-label="Failed attempts by error kind">
                  {counters.errorKinds.map((k) => (
                    <li key={k.kind} className="flex justify-between gap-2"><span className="truncate font-mono">{k.kind}</span><span className="tabular-nums">{formatInteger(k.count)}</span></li>
                  ))}
                </ul>
              </CountList>
            </div>
            <p className="text-[11px] text-slate-400">A retry repeats the same model after a transient error; a fallback moves to another model. With several backend instances each reports its own counters.</p>
          </div>
        )}
      </CardBody>
    </Card>
  )
}
