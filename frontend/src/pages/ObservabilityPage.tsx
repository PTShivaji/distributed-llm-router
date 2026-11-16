import { useState } from 'react'
import { BarChart3, Info, RefreshCw } from 'lucide-react'

import { api } from '../api/endpoints'
import { ApiError } from '../api/errors'
import type { DependenciesResponse } from '../api/types'
import { EmptyState, SkeletonRows } from '../components/feedback/States'
import { ConnectionStatus } from '../components/layout/ConnectionStatus'
import { HealthCard } from '../components/observability/HealthCard'
import { FamiliesPanel, HttpPanel, MetricsKpis, RoutingPanel, SafeguardsPanel, UsagePanel } from '../components/observability/MetricsPanels'
import { SectionFailure } from '../components/observability/StaleNotice'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardBody, CardHeader } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { useApiQuery } from '../hooks/useApiQuery'
import { refreshBackendHealth, useBackendHealth } from '../hooks/useBackendHealth'
import { formatClock } from '../lib/format'
import { isDependenciesResponse, metricsView, parseExposition, type MetricsView } from '../lib/observability'

interface Loaded<T> { value: T; at: number; traceId: string | null }

function malformed(message: string, status: number): ApiError {
  return new ApiError({ message, status, code: 'malformed_response', kind: 'unknown' })
}

/**
 * Observability: GET /health (public), GET /api/v1/health/dependencies and GET /metrics (both
 * behind OBSERVABILITY_API_TOKEN when configured). Loaded when the page opens and on Refresh only:
 * never polled, never retried automatically. A failed refresh keeps the earlier data, marked stale.
 */
export function ObservabilityPage() {
  const [refreshKey, setRefreshKey] = useState(0)
  const health = useBackendHealth()

  const deps = useApiQuery<Loaded<DependenciesResponse>>(async (signal) => {
    const { data, meta } = await api.dependencies({ signal })
    if (!isDependenciesResponse(data)) throw malformed('GET /api/v1/health/dependencies returned a body that does not match the dependency schema.', meta.status)
    return { data: { value: data, at: Date.now(), traceId: meta.traceId }, meta }
  }, refreshKey)

  const metrics = useApiQuery<Loaded<MetricsView>>(async (signal) => {
    const { data, meta } = await api.metricsText({ signal })
    if (typeof data !== 'string') throw malformed('GET /metrics did not return Prometheus text.', meta.status)
    const view = metricsView(parseExposition(data))
    if (!view.recognised) throw malformed('GET /metrics answered, but contained no llm_router_* metrics.', meta.status)
    return { data: { value: view, at: Date.now(), traceId: meta.traceId }, meta }
  }, refreshKey)

  const refresh = () => {
    setRefreshKey((k) => k + 1)
    void refreshBackendHealth()
  }

  const loading = deps.loading || metrics.loading
  const failed = deps.error || metrics.error
  const lastSuccess = Math.max(deps.data?.at ?? 0, metrics.data?.at ?? 0)
  const status = loading ? 'Loading…'
    : failed ? (deps.data || metrics.data ? 'Refresh failed · showing earlier data' : 'Not loaded')
      : lastSuccess ? `Updated ${formatClock(new Date(lastSuccess))}` : 'Loading…'
  const view = metrics.data?.value ?? null
  const bothUnauthorized = deps.error?.kind === 'unauthorized' && metrics.error?.kind === 'unauthorized'
  const traceId = metrics.data?.traceId ?? deps.data?.traceId ?? null

  return (
    <div className="space-y-6">
      <PageHeader
        title="Observability"
        description="Service and dependency health, plus the backend's Prometheus metrics for this process."
        actions={(
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span className="hidden sm:inline-flex"><ConnectionStatus /></span>
            <span className="text-xs text-slate-500" aria-live="polite" data-testid="refresh-status">{status}</span>
            <Button variant="secondary" size="sm" onClick={refresh} loading={loading}
              icon={<RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />}>Refresh</Button>
          </div>
        )}
      />

      <p className="flex gap-2 rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-600 shadow-sm" role="note" data-testid="process-note">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />
        <span>
          <strong className="font-medium text-slate-700">Per process, since start:</strong> every metric below is cumulative since the backend
          process that answered started, and resets when it restarts. With several instances each keeps its own counters, and requests can
          reach different instances, so these are not cluster totals. This page&apos;s own requests are counted too. Loaded on open and on
          Refresh only; nothing polls.
        </span>
      </p>

      <HealthCard liveness={health.status} health={health.data} healthError={health.error}
        deps={deps.data?.value ?? null} depsAt={deps.data?.at ?? null} depsError={deps.error}
        depsLoading={deps.loading} onRetry={refresh} />

      <section aria-labelledby="metrics-heading" className="space-y-4">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <h2 id="metrics-heading" className="text-sm font-semibold text-slate-900">Application metrics</h2>
          <span className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
            {metrics.data && <span data-testid="metrics-at">Scraped {formatClock(new Date(metrics.data.at))}</span>}
            {view?.build && <Badge tone={view.build.mockMode === 'true' ? 'demo' : 'neutral'}>v{view.build.version} · mock mode {view.build.mockMode}</Badge>}
          </span>
        </div>

        {metrics.error && (
          !metrics.data && metrics.error.kind === 'not_found' ? (
            <Card data-testid="metrics-disabled"><EmptyState icon={<BarChart3 className="h-6 w-6" aria-hidden="true" />} title="Metrics are disabled on this backend"
              description="GET /metrics returned 404, which is what the backend does when METRICS_ENABLED=false. No metric values are available; dependency health above is unaffected." /></Card>
          ) : <SectionFailure error={metrics.error} hasData={metrics.data !== null} at={metrics.data?.at ?? null} what="metrics"
            onRetry={refresh} prompt={!bothUnauthorized} />
        )}

        {!view && metrics.loading && !metrics.error && <Card><CardBody><SkeletonRows rows={5} /></CardBody></Card>}
        {view && (
          <>
            <MetricsKpis view={view} />
            <HttpPanel view={view} />
            <RoutingPanel view={view} />
            <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
              <UsagePanel view={view} />
              <SafeguardsPanel view={view} />
            </div>
            <FamiliesPanel view={view} />
          </>
        )}
      </section>

      <Card>
        <CardHeader title="Reading these numbers" description="What the backend exposes, and what it does not" />
        <CardBody className="grid grid-cols-1 gap-4 text-sm text-slate-700 md:grid-cols-2" data-testid="interpretation">
          <div>
            <h3 className="font-semibold text-slate-900">Counters and gauges</h3>
            <p className="text-slate-600">Counters (<code className="font-mono text-xs">*_total</code>) only grow while a process runs and start again
              from nothing after a restart, so they show totals since start, not a rate or a recent window. This page draws no time series from them.
              Gauges (in flight, hit ratio) are the value at the moment of the scrape.</p>
          </div>
          <div>
            <h3 className="font-semibold text-slate-900">Histograms</h3>
            <p className="text-slate-600">Latency histograms count observations per bucket (seconds, cumulative). The mean is exact; a percentile can only be
              placed in a bucket, so p50/p95 are shown as the bucket&apos;s range, never interpolated into a precise-looking number.</p>
          </div>
          <div>
            <h3 className="font-semibold text-slate-900">Missing is not zero</h3>
            <p className="text-slate-600"><em>Not exposed</em>: the backend does not publish that metric. <em>None recorded</em>: it is published but no labelled
              series exists yet. A number (including 0) is a value the backend actually reported. <em>Undefined</em>: the hit ratio before any lookup.</p>
          </div>
          <div>
            <h3 className="font-semibold text-slate-900">Estimates and instances</h3>
            <p className="text-slate-600">Estimated cost uses registry prices (placeholders in mock mode); it is not an invoice. For cluster-wide figures,
              let Prometheus scrape every instance and aggregate there (for example <code className="font-mono text-xs">sum by (route)</code>).</p>
          </div>
          <div className="md:col-span-2">
            <h3 className="font-semibold text-slate-900">Tracing</h3>
            <p className="text-slate-600" data-testid="tracing-status">
              The API does not expose tracing settings. When tracing is enabled and a request is sampled, the backend adds an{' '}
              <code className="font-mono text-xs">X-Trace-ID</code> header.{' '}
              {traceId
                ? <>The last response carried one (<code className="font-mono text-xs">{traceId}</code>), so spans are being recorded.</>
                : (deps.data || metrics.data) ? 'The last responses carried none: tracing is disabled, or those requests were not sampled.' : 'No response has been received yet.'}
            </p>
          </div>
        </CardBody>
      </Card>
    </div>
  )
}
