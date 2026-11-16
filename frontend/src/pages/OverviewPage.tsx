import { useCallback, useMemo, useState } from 'react'
import {
  Activity, CheckCircle2, Coins, Gauge, Layers, RefreshCw, Shuffle, Timer, XCircle,
} from 'lucide-react'

import { api } from '../api/endpoints'
import { ModelUsageChart } from '../components/charts/ModelUsageChart'
import { RequestActivityChart } from '../components/charts/RequestActivityChart'
import { SectionError } from '../components/feedback/SectionError'
import { EmptyState, SkeletonRows } from '../components/feedback/States'
import { ConnectionStatus } from '../components/layout/ConnectionStatus'
import { CacheSummaryCard } from '../components/overview/CacheSummaryCard'
import { ProviderHealthCard } from '../components/overview/ProviderHealthCard'
import { RecentActivityCard } from '../components/overview/RecentActivityCard'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardBody, CardHeader } from '../components/ui/Card'
import { KpiCard } from '../components/ui/KpiCard'
import { PageHeader } from '../components/ui/PageHeader'
import { useApiQuery } from '../hooks/useApiQuery'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { refreshBackendHealth } from '../hooks/useBackendHealth'
import { formatClock, formatInteger, formatLatency, formatPercent, formatUsd } from '../lib/format'
import { fetchHourlyActivity, HOURS } from '../lib/hourly'
import { modelUsageRows } from '../lib/overview'

/** KPIs, models and cache stats: 3 light requests per minute while the tab is visible. */
export const AUTO_REFRESH_MS = 60_000

function share(part: number, total: number): string | undefined {
  return total > 0 ? `${formatPercent(part / total)} of total` : undefined
}

export function OverviewPage() {
  const [refreshKey, setRefreshKey] = useState(0)
  const [chartKey, setChartKey] = useState(0)
  // GET /cache/stats prunes and trims the cache on every read (Step 10.5B): loaded on open and on manual Refresh only, never auto-polled.
  const [cacheKey, setCacheKey] = useState(0)
  const [autoRefresh, setAutoRefresh] = useState(true)

  const summary = useApiQuery((signal) => api.analyticsSummary({}, { signal }), refreshKey)
  const models = useApiQuery((signal) => api.models({ signal }), refreshKey)
  const cache = useApiQuery((signal) => api.cacheStats({ signal }), cacheKey)
  // 24 hourly aggregate windows: loaded on open and on manual refresh only (not auto-polled).
  const hourly = useApiQuery((signal) => fetchHourlyActivity(api, signal).then((data) => ({ data })), chartKey)

  const refreshAll = useCallback(() => {
    setRefreshKey((k) => k + 1)
    setChartKey((k) => k + 1)
    setCacheKey((k) => k + 1)
    void refreshBackendHealth()
  }, [])

  // Pause auto-refresh while any 429 Retry-After window is still open.
  const pausedUntil = Math.max(0, ...[summary, models, cache, hourly].map((q) =>
    q.error?.retryAfterSeconds != null && q.updatedAt ? q.updatedAt + q.error.retryAfterSeconds * 1000 : 0))
  useAutoRefresh(() => setRefreshKey((k) => k + 1), { intervalMs: AUTO_REFRESH_MS, enabled: autoRefresh, pausedUntil })

  const s = summary.data
  const m = models.data
  const loading = summary.loading || models.loading || cache.loading
  const lastUpdated = Math.max(0, ...[summary, models, cache].map((q) => (q.error ? 0 : (q.updatedAt ?? 0))))
  const enabledModels = m ? m.models.filter((x) => x.enabled).length : null
  const usageRows = useMemo(() => (s ? modelUsageRows(s.requests_by_model, m) : []), [s, m])
  const placeholderPricing = Boolean(m && (m.mock_mode || m.models.some((x) => x.pricing_is_placeholder &&
    usageRows.some((r) => r.model === x.id))))
  const kpiLoading = summary.loading && !s
  const hourlyEmpty = hourly.data ? hourly.data.every((p) => p.total === 0) : false

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description="Requests, latency, estimated cost, cache efficiency and model health, from the router's own API."
        actions={(
          <div className="flex flex-wrap items-center justify-end gap-2">
            {/* the top bar already shows it on phones */}
            <span className="hidden sm:inline-flex"><ConnectionStatus /></span>
            <span className="text-xs text-slate-500" aria-live="polite">
              {lastUpdated ? `Updated ${formatClock(new Date(lastUpdated))}` : 'Not updated yet'}
            </span>
            <label className="flex items-center gap-1.5 text-xs text-slate-600">
              <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
              Auto-refresh (60 s)
            </label>
            <Button variant="secondary" size="sm" onClick={refreshAll} loading={loading}
              icon={<RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />}>Refresh</Button>
          </div>
        )}
      />

      {/* B. KPI cards */}
      <section aria-labelledby="kpi-heading" className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <h2 id="kpi-heading" className="shrink-0 text-sm font-semibold text-slate-900">Key metrics</h2>
          <span className="text-xs text-slate-500">All persisted request history · GET /api/v1/analytics/summary</span>
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <KpiCard label="Total requests" icon={<Activity className="h-4 w-4" />} tone="brand" loading={kpiLoading}
            value={formatInteger(s?.total_requests)} />
          <KpiCard label="Successful" icon={<CheckCircle2 className="h-4 w-4" />} tone="success" loading={kpiLoading}
            value={formatInteger(s?.successful_requests)} hint={s ? share(s.successful_requests, s.total_requests) : undefined} />
          <KpiCard label="Failed" icon={<XCircle className="h-4 w-4" />} tone="danger" loading={kpiLoading}
            value={formatInteger(s?.failed_requests)} hint={s ? share(s.failed_requests, s.total_requests) : undefined} />
          <KpiCard label="Average latency" icon={<Timer className="h-4 w-4" />} loading={kpiLoading}
            value={formatLatency(s?.average_latency_ms)}
            hint={s ? (s.average_latency_ms === null ? 'No requests yet' : 'End-to-end, per request') : undefined} />
          <KpiCard label="Estimated cost" icon={<Coins className="h-4 w-4" />} tone="warning" loading={kpiLoading}
            value={formatUsd(s?.estimated_total_cost)}
            hint={s ? (placeholderPricing ? 'Estimate · placeholder pricing' : 'Estimate from registry prices') : undefined} />
          <KpiCard label="Cache hit rate" icon={<Gauge className="h-4 w-4" />} tone="brand" loading={kpiLoading}
            value={formatPercent(s?.cache_hit_rate)}
            hint={s ? `${formatInteger(s.cache_hits)} hits · ${formatInteger(s.cache_misses)} misses` : undefined} />
          <KpiCard label="Active models" icon={<Layers className="h-4 w-4" />} loading={models.loading && !m}
            value={enabledModels === null ? '—' : `${enabledModels} / ${m!.models.length}`}
            hint={m ? `enabled in registry${m.mock_mode ? ' · mock mode' : ''}` : undefined} />
          <KpiCard label="Fallback rate" icon={<Shuffle className="h-4 w-4" />} loading={kpiLoading}
            value={formatPercent(s?.fallback_rate)} hint={s ? `${formatInteger(s.fallback_requests)} requests used a fallback` : undefined} />
        </div>
        {summary.error && <SectionError error={summary.error} credential="observability" onRetry={refreshAll} />}
      </section>

      {/* C. Request activity + F. Cache summary */}
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader title={`Request activity · last ${HOURS} hours`}
            description="Aggregated API data: one analytics summary per UTC hour window, labelled in your local time. Refreshed on open and with Refresh."
            actions={<Badge tone="info">Aggregated API data</Badge>} />
          <CardBody>
            {hourly.error ? <SectionError error={hourly.error} credential="observability" onRetry={refreshAll} />
              : !hourly.data ? <SkeletonRows rows={5} />
                : hourlyEmpty ? <EmptyState title={`No requests in the last ${HOURS} hours`}
                  description="Nothing has been recorded in this period. Send a request from the Chat Playground to see activity." />
                  : <RequestActivityChart points={hourly.data} />}
          </CardBody>
        </Card>
        <div className="min-w-0">
          {cache.error ? (
            <Card><CardHeader title="Response cache" description="From GET /api/v1/cache/stats" />
              <CardBody><SectionError error={cache.error} credential="observability" onRetry={refreshAll} /></CardBody></Card>
          ) : cache.data ? <CacheSummaryCard stats={cache.data} /> : (
            <Card><CardHeader title="Response cache" /><CardBody><SkeletonRows rows={4} /></CardBody></Card>
          )}
        </div>
      </div>

      {/* D. Model usage + E. Provider health */}
      <div className="grid grid-cols-1 gap-6 2xl:grid-cols-2">
        <Card>
          <CardHeader title="Model usage" description="Requests by model actually used · analytics requests_by_model, providers from the registry" />
          <CardBody>
            {summary.error ? <p className="text-sm text-slate-500">Unavailable: analytics could not be loaded (see above).</p>
              : !s ? <SkeletonRows rows={4} />
                : usageRows.length === 0 ? <EmptyState title="No model usage recorded yet" description="No persisted requests have been served by a model." />
                  : <ModelUsageChart rows={usageRows} />}
          </CardBody>
        </Card>
        {models.error ? (
          <Card><CardHeader title="Provider health" /><CardBody><SectionError error={models.error} onRetry={models.reload} /></CardBody></Card>
        ) : m ? <ProviderHealthCard data={m} /> : (
          <Card><CardHeader title="Provider health" /><CardBody><SkeletonRows rows={5} /></CardBody></Card>
        )}
      </div>

      {/* G. Recent activity (this browser session only) */}
      <RecentActivityCard />
    </div>
  )
}
