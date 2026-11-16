import type { ReactNode } from 'react'
import { AlertTriangle, CheckCircle2, HelpCircle, MinusCircle, XCircle } from 'lucide-react'

import type { ApiError } from '../../api/errors'
import type { DependenciesResponse, HealthResponse } from '../../api/types'
import { formatClock, formatInteger } from '../../lib/format'
import { dependencyState, overallState, type DependencyState, type Liveness, type OverallState } from '../../lib/observability'
import { SkeletonRows } from '../feedback/States'
import { Badge, type BadgeTone } from '../ui/Badge'
import { Card, CardBody, CardHeader } from '../ui/Card'
import { SectionFailure } from './StaleNotice'

type State = DependencyState | OverallState
const STATE: Record<State, { label: string; tone: BadgeTone; icon: ReactNode }> = {
  healthy: { label: 'Healthy', tone: 'success', icon: <CheckCircle2 className="h-3 w-3" aria-hidden="true" /> },
  degraded: { label: 'Degraded', tone: 'warning', icon: <AlertTriangle className="h-3 w-3" aria-hidden="true" /> },
  unavailable: { label: 'Unavailable', tone: 'danger', icon: <XCircle className="h-3 w-3" aria-hidden="true" /> },
  disabled: { label: 'Disabled', tone: 'neutral', icon: <MinusCircle className="h-3 w-3" aria-hidden="true" /> },
  unknown: { label: 'Unknown', tone: 'neutral', icon: <HelpCircle className="h-3 w-3" aria-hidden="true" /> },
}

export function StateBadge({ state, testId }: { state: State; testId?: string }) {
  const s = STATE[state]
  return <span data-testid={testId} data-state={state}><Badge tone={s.tone} icon={s.icon}>{s.label}</Badge></span>
}

function formatUptime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const d = Math.floor(s / 86_400)
  const h = Math.floor((s % 86_400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h ${m}m`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m ${s % 60}s`
}

function Row({ name, source, state, children, testId }: { name: string; source: string; state?: State; children?: ReactNode; testId: string }) {
  return (
    <li className="flex flex-col gap-1 px-3 py-2.5 sm:flex-row sm:items-start sm:justify-between sm:gap-4" data-testid={testId}>
      <div className="min-w-0">
        <p className="text-sm font-medium text-slate-900">{name}</p>
        <p className="text-[11px] text-slate-400">{source}</p>
      </div>
      <div className="flex min-w-0 flex-col gap-1 text-xs text-slate-600 sm:items-end sm:text-right">
        {state && <StateBadge state={state} />}
        {children}
      </div>
    </li>
  )
}

const DEP_TEXT: Record<'redis' | 'database', Record<DependencyState, string>> = {
  redis: {
    healthy: 'PING answered.',
    unavailable: 'Enabled but PING failed. Cache lookups, shared rate limits and the Redis health store are affected.',
    disabled: 'REDIS_ENABLED=false: cache, shared rate limits and shared health are off.',
    unknown: 'Not checked.',
  },
  database: {
    healthy: 'Connection check succeeded.',
    unavailable: 'Enabled but not reachable. Request logs are dropped and date-ranged analytics fail.',
    disabled: 'DATABASE_ENABLED=false: request logs are not persisted.',
    unknown: 'Not checked.',
  },
}

/**
 * Service health: liveness from GET /health (public, touches no dependency) and readiness detail
 * from GET /api/v1/health/dependencies (observability token; pings Redis and PostgreSQL).
 */
export function HealthCard({ liveness, health, healthError, deps, depsAt, depsError, depsLoading, onRetry }: {
  liveness: Liveness
  health: HealthResponse | null
  healthError: ApiError | null
  deps: DependenciesResponse | null
  depsAt: number | null
  depsError: ApiError | null
  depsLoading: boolean
  onRetry: () => void
}) {
  // stale dependency data (from before a failed refresh) is shown but never decides the overall state
  const current = depsError ? null : deps
  const overall = depsLoading && !depsError ? { state: 'unknown' as const, reasons: ['Checking dependencies…'] } : overallState(liveness, current)
  const dep = (d: 'redis' | 'database') => (deps ? dependencyState(deps[d]) : 'unknown')

  return (
    <Card data-testid="health-card">
      <CardHeader title="Service and dependency health"
        description={depsAt ? `Dependencies last checked ${formatClock(new Date(depsAt))}` : 'GET /health · GET /api/v1/health/dependencies'}
        actions={<StateBadge state={overall.state} testId="overall-state" />} />
      <CardBody className="space-y-3">
        {overall.reasons.length > 0 && overall.state !== 'unknown' && (
          <ul className="space-y-1 text-xs text-amber-900" data-testid="overall-reasons">
            {overall.reasons.map((r) => <li key={r} className="rounded-md bg-amber-50 px-2.5 py-1.5">{r}</li>)}
          </ul>
        )}
        {depsError && <SectionFailure error={depsError} hasData={deps !== null} at={depsAt} what="dependency health" onRetry={onRetry} />}
        {!deps && depsLoading && !depsError ? <SkeletonRows rows={4} /> : (
          <ul className="divide-y divide-slate-100 rounded-md border border-slate-200" aria-label="Components">
            <Row name="API process" source="GET /health (liveness)" testId="dep-api"
              state={liveness === 'connected' ? 'healthy' : liveness === 'unreachable' ? 'unavailable' : 'unknown'}>
              {health ? (
                <span>v{health.version} · {health.environment} · up {formatUptime(health.uptime_seconds)}</span>
              ) : healthError ? <span>{healthError.message}</span> : <span>Checking…</span>}
            </Row>
            {deps && (
              <>
                <Row name="Redis" source="PING" state={dep('redis')} testId="dep-redis">
                  <span>{DEP_TEXT.redis[dep('redis')]}</span>
                </Row>
                <Row name="PostgreSQL" source="Connection check" state={dep('database')} testId="dep-database">
                  <span>{DEP_TEXT.database[dep('database')]}</span>
                </Row>
                <Row name="Response cache" source="cache_enabled" testId="dep-cache">
                  <Badge tone={deps.cache_enabled ? 'success' : 'neutral'}>{deps.cache_enabled ? 'Configured' : 'Not configured'}</Badge>
                  {deps.cache_enabled && <span>Uses Redis; its reachability is the Redis row above.</span>}
                </Row>
                <Row name="Rate limiting" source="rate_limit_enabled" testId="dep-ratelimit">
                  <Badge tone={deps.rate_limit_enabled ? 'success' : 'neutral'}>{deps.rate_limit_enabled ? 'Configured' : 'Not configured'}</Badge>
                </Row>
                <Row name="Provider health store" source="health_backend" testId="dep-health-backend">
                  <span className="font-mono">{deps.health_backend}</span>
                  <span>{deps.health_backend === 'redis' ? 'Shared by all instances through Redis.'
                    : deps.health_backend === 'memory' ? 'Per process: each instance tracks provider health on its own.' : 'Reported by the backend.'}</span>
                </Row>
                <Row name="Request-log persistence" source="persistence (this process)" testId="dep-persistence">
                  {deps.database.enabled ? (
                    <dl className="flex flex-wrap gap-x-3 gap-y-0.5 sm:justify-end">
                      {Object.entries(deps.persistence).map(([k, v]) => (
                        <div key={k} className="flex gap-1"><dt>{k.charAt(0).toUpperCase() + k.slice(1)}</dt><dd className="font-semibold tabular-nums text-slate-900">{formatInteger(v)}</dd></div>
                      ))}
                    </dl>
                  ) : <span className="text-slate-400">Not applicable: the database is disabled, so these counters are defaults, not measurements.</span>}
                </Row>
              </>
            )}
          </ul>
        )}
      </CardBody>
    </Card>
  )
}
