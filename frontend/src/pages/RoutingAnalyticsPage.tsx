import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import {
  Activity, AlertTriangle, CheckCircle2, Coins, Gauge, Hash, Info, RefreshCw, Shuffle, Timer, XCircle,
} from 'lucide-react'

import { api } from '../api/endpoints'
import type { AnalyticsSummaryResponse } from '../api/types'
import { ProcessCountersCard } from '../components/analytics/ProcessCountersCard'
import { RangeFilter } from '../components/analytics/RangeFilter'
import { ComplexityChart } from '../components/charts/ComplexityChart'
import { ModelUsageChart } from '../components/charts/ModelUsageChart'
import { ProviderAttemptsChart } from '../components/charts/ProviderAttemptsChart'
import { SectionError } from '../components/feedback/SectionError'
import { EmptyState, SkeletonRows } from '../components/feedback/States'
import { ConnectionStatus } from '../components/layout/ConnectionStatus'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardBody, CardHeader } from '../components/ui/Card'
import { KpiCard } from '../components/ui/KpiCard'
import { PageHeader } from '../components/ui/PageHeader'
import { useApiQuery } from '../hooks/useApiQuery'
import { refreshBackendHealth, useBackendHealth } from '../hooks/useBackendHealth'
import {
  attemptTotals, complexityRows, providerAttemptRows, rangeQuery, readRange, resolveRange, usageSummary,
  usesPlaceholderPricing, writeRange, RANGE_PRESETS, type RangeSelection,
} from '../lib/analytics'
import { formatClock, formatInteger, formatLatency, formatPercent, formatUsd } from '../lib/format'
import { modelUsageRows } from '../lib/overview'
import { parsePrometheus, routerCounters } from '../lib/prometheus'

interface RangedSummary {
  /** Identifies the selected range this result belongs to (so a failed range change never shows another range's data). */
  rangeId: string
  summary: AnalyticsSummaryResponse
}

const dateTime = (iso: string) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
const share = (part: number, whole: number) => (whole > 0 ? `${formatPercent(part / whole)} of requests` : undefined)

function windowLabel(s: AnalyticsSummaryResponse): string {
  if (!s.start && !s.end) return 'All persisted request history'
  if (s.start && s.end) return `${dateTime(s.start)} → ${dateTime(s.end)}`
  return s.start ? `From ${dateTime(s.start)}` : `Until ${dateTime(s.end!)}`
}

/**
 * Routing Analytics.
 *   GET /api/v1/analytics/summary?start&end  aggregates of persisted request history (PostgreSQL), per range
 *   GET /api/v1/models                         provider + pricing flags for the joined tables
 *   GET /metrics                               retry/fallback counters since PROCESS start (not ranged)
 * Loaded on open, on range change (analytics only) and with Refresh. No polling.
 */
export function RoutingAnalyticsPage() {
  const [params, setParams] = useSearchParams()
  const selection = readRange(params)
  const rangeId = JSON.stringify(selection)
  const [refreshKey, setRefreshKey] = useState(0)

  const analytics = useApiQuery<RangedSummary>(async (signal) => {
    // rolling presets end at the moment of the request
    const { data, meta } = await api.analyticsSummary(rangeQuery(resolveRange(selection, Date.now())), { signal })
    return { data: { rangeId, summary: data }, meta }
  }, `${rangeId}#${refreshKey}`)
  const models = useApiQuery((signal) => api.models({ signal }), refreshKey)
  const metrics = useApiQuery((signal) => api.metricsText({ signal }), refreshKey)
  const health = useBackendHealth()

  const current = analytics.data?.rangeId === rangeId ? analytics.data.summary : null
  const counters = useMemo(() => (metrics.data ? routerCounters(parsePrometheus(metrics.data)) : null), [metrics.data])

  const setRange = useCallback((sel: RangeSelection) => setParams((prev) => writeRange(prev, sel), { replace: true }), [setParams])
  const refresh = () => {
    setRefreshKey((k) => k + 1)
    void refreshBackendHealth()
  }

  const s = current
  const m = models.data
  const kpiLoading = !s && !analytics.error
  const usage = s ? usageSummary(s) : null
  const complexity = s ? complexityRows(s.requests_by_complexity, s.total_requests) : []
  const modelRows = useMemo(() => (s ? modelUsageRows(s.requests_by_model, m) : []), [s, m])
  const attempts = s ? providerAttemptRows(s.provider_attempts) : []
  const totals = attemptTotals(attempts)
  const placeholder = s ? usesPlaceholderPricing(s, m) : false
  const presetLabel = selection.key === 'custom' ? 'Custom range' : RANGE_PRESETS.find((p) => p.key === selection.key)?.label
  const status = analytics.error
    ? (s ? 'Refresh failed · showing earlier data' : 'Not loaded')
    : analytics.loading ? 'Loading…' : analytics.updatedAt ? `Updated ${formatClock(new Date(analytics.updatedAt))}` : 'Loading…'

  return (
    <div className="space-y-6">
      <PageHeader
        title="Routing Analytics"
        description="Requests, outcomes, latency, tokens and estimated cost from the router's persisted request history, broken down by complexity, model and provider."
        actions={(
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span className="hidden sm:inline-flex"><ConnectionStatus /></span>
            <span className="text-xs text-slate-500" aria-live="polite">{status}</span>
            <Button variant="secondary" size="sm" onClick={refresh} loading={analytics.loading || metrics.loading}
              icon={<RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />}>Refresh</Button>
          </div>
        )}
      />

      {/* Range */}
      <Card>
        <CardBody className="space-y-3">
          <RangeFilter key={selection.key === 'custom' ? rangeId : 'preset'} selection={selection} onChange={setRange} />
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500" data-testid="range-window">
            <Badge tone="info">{presetLabel}</Badge>
            {s ? <span>{windowLabel(s)}{s.end ? ' (end exclusive)' : ''} · your local time</span> : <span>{analytics.error ? 'Range not loaded' : 'Loading…'}</span>}
            <span className="text-slate-400">· GET /api/v1/analytics/summary</span>
          </p>
        </CardBody>
      </Card>

      {/* KPIs */}
      <section aria-labelledby="analytics-kpis" className="space-y-3">
        <h2 id="analytics-kpis" className="text-sm font-semibold text-slate-900">Requests in this range</h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <KpiCard label="Total requests" icon={<Activity className="h-4 w-4" />} tone="brand" loading={kpiLoading}
            value={formatInteger(s?.total_requests)} />
          <KpiCard label="Successful" icon={<CheckCircle2 className="h-4 w-4" />} tone="success" loading={kpiLoading}
            value={formatInteger(s?.successful_requests)} hint={s ? share(s.successful_requests, s.total_requests) : undefined} />
          <KpiCard label="Failed" icon={<XCircle className="h-4 w-4" />} tone="danger" loading={kpiLoading}
            value={formatInteger(s?.failed_requests)} hint={s ? share(s.failed_requests, s.total_requests) : undefined} />
          <KpiCard label="Average latency" icon={<Timer className="h-4 w-4" />} loading={kpiLoading}
            value={formatLatency(s?.average_latency_ms)} hint={s ? (s.average_latency_ms === null ? 'No requests in range' : 'End-to-end, per request') : undefined} />
          <KpiCard label="Estimated cost" icon={<Coins className="h-4 w-4" />} tone="warning" loading={kpiLoading}
            value={formatUsd(s?.estimated_total_cost)} hint={s ? (placeholder ? 'Estimate · placeholder pricing' : 'Estimate from registry prices') : undefined} />
          <KpiCard label="Cache hit rate" icon={<Gauge className="h-4 w-4" />} tone="brand" loading={kpiLoading}
            value={formatPercent(s?.cache_hit_rate)}
            hint={s ? (s.cache_hit_rate === null ? 'No cache lookups in range' : `${formatInteger(s.cache_hits)} hits · ${formatInteger(s.cache_misses)} misses`) : undefined} />
          <KpiCard label="Fallback rate" icon={<Shuffle className="h-4 w-4" />} loading={kpiLoading}
            value={formatPercent(s?.fallback_rate)} hint={s ? `${formatInteger(s.fallback_requests)} requests used a fallback` : undefined} />
          <KpiCard label="Total tokens" icon={<Hash className="h-4 w-4" />} loading={kpiLoading}
            value={formatInteger(s?.total_tokens)}
            hint={s ? `${formatInteger(s.total_input_tokens)} in · ${formatInteger(s.total_output_tokens)} out` : undefined} />
        </div>
        {analytics.error && <SectionError error={analytics.error} credential="observability" onRetry={refresh} />}
        {s && (
          <p className="flex gap-2 text-xs text-slate-500">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />
            Cache hit rate counts hits ÷ (hits + misses); requests that bypassed the cache or ran with it disabled are excluded.
          </p>
        )}
      </section>

      {/* Breakdowns */}
      {!s ? (
        analytics.error ? null : <Card><CardBody><SkeletonRows rows={6} /></CardBody></Card>
      ) : s.total_requests === 0 ? (
        <Card>
          <EmptyState title="No requests in this range" description="Nothing was recorded in the selected window. Choose a wider range, or send a request from the Chat Playground." />
        </Card>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
            <Card>
              <CardHeader title="Requests by complexity" description="requests_by_complexity · classification of each persisted request" />
              <CardBody>
                {complexity.length === 0 ? <EmptyState title="No complexity data" /> : <ComplexityChart rows={complexity} />}
              </CardBody>
            </Card>
            <Card>
              <CardHeader title="Requests by model" description="Model that actually answered (after any fallback) · providers from the registry" />
              <CardBody>
                {modelRows.length === 0
                  ? <EmptyState title="No model usage" description="No request in this range was answered by a model (for example, all failed)." />
                  : <ModelUsageChart rows={modelRows} />}
                {models.error && !m && <p className="mt-2 text-xs text-slate-500">Model registry unavailable ({models.error.message}): provider names cannot be shown.</p>}
              </CardBody>
            </Card>
          </div>

          <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
            <Card>
              <CardHeader title="Provider attempts" description="Every attempt made in this range, including retries and fallbacks; skipped = candidate not called (e.g. unhealthy, budget or rate limit)" />
              <CardBody className="space-y-4">
                <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
                  {([
                    ['Requests with fallback', formatInteger(s.fallback_requests)],
                    ['Fallback rate', formatPercent(s.fallback_rate)],
                    ['Failed attempts', formatInteger(totals.failures)],
                    ['Skipped candidates', formatInteger(totals.skipped)],
                  ] as const).map(([label, value]) => (
                    <div key={label} className="rounded-md bg-slate-50 px-3 py-2">
                      <dt className="text-slate-500">{label}</dt>
                      <dd className="text-base font-semibold tabular-nums text-slate-900" data-testid={`stat-${label}`}>{value}</dd>
                    </div>
                  ))}
                </dl>
                {attempts.length === 0
                  ? <EmptyState title="No provider attempts" description="No provider was attempted in this range (for example, every request was a cache hit)." />
                  : <ProviderAttemptsChart rows={attempts} totals={totals} />}
              </CardBody>
            </Card>

            <Card>
              <CardHeader title="Token usage and estimated cost" description="Sums over requests in this range; averages are per request" />
              <CardBody className="space-y-4" data-testid="usage-card">
                <dl className="divide-y divide-slate-100 rounded-md border border-slate-200 text-sm">
                  {([
                    ['Input tokens', formatInteger(usage!.inputTokens)],
                    ['Output tokens', formatInteger(usage!.outputTokens)],
                    ['Total tokens', formatInteger(usage!.totalTokens)],
                    ['Average tokens per request', usage!.tokensPerRequest === null ? '—' : formatInteger(Math.round(usage!.tokensPerRequest))],
                    ['Estimated total cost', formatUsd(usage!.estimatedCost)],
                    ['Average estimated cost per request', formatUsd(usage!.costPerRequest)],
                  ] as const).map(([label, value]) => (
                    <div key={label} className="flex justify-between gap-4 px-3 py-2">
                      <dt className="text-slate-500">{label}</dt>
                      <dd className="tabular-nums text-slate-900">{value}</dd>
                    </div>
                  ))}
                </dl>
                {usage!.inputShare !== null && (
                  <div className="space-y-1.5">
                    <div className="flex h-2.5 overflow-hidden rounded-full bg-slate-100" role="img"
                      aria-label={`Input ${formatPercent(usage!.inputShare)} and output ${formatPercent(1 - usage!.inputShare)} of tokens`}>
                      <div className="h-full bg-brand-600" style={{ width: `${usage!.inputShare * 100}%` }} />
                      <div className="h-full border-l-2 border-white bg-brand-300" style={{ width: `${(1 - usage!.inputShare) * 100}%` }} />
                    </div>
                    <p className="flex justify-between text-[11px] text-slate-500">
                      <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-brand-600" aria-hidden="true" />Input {formatPercent(usage!.inputShare)}</span>
                      <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-brand-300" aria-hidden="true" />Output {formatPercent(1 - usage!.inputShare)}</span>
                    </p>
                  </div>
                )}
                {placeholder && (
                  <p className="flex gap-2 rounded-md bg-amber-50 p-2.5 text-xs text-amber-900">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" aria-hidden="true" />
                    Costs use placeholder pricing{m?.mock_mode ? ' (mock mode)' : ''}: estimates only, not verified provider prices.
                  </p>
                )}
                <p className="text-xs text-slate-500">
                  Spend limits and remaining budget are enforced by the backend and are not exposed by this API, so they are not shown here.
                  {s.total_tokens === 0 && ' No tokens were recorded (for example, cache hits or failures).'}
                </p>
              </CardBody>
            </Card>
          </div>
        </>
      )}

      {/* Process counters (not ranged) */}
      <ProcessCountersCard counters={counters} error={metrics.data ? null : metrics.error} loading={metrics.loading}
        uptimeSeconds={health.data?.uptime_seconds ?? null} onRetry={refresh} />
    </div>
  )
}
