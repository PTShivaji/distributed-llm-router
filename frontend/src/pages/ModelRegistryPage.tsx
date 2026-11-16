import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router'
import { Boxes, CheckCircle2, Info, Layers, RefreshCw, Search, XCircle } from 'lucide-react'

import { api } from '../api/endpoints'
import { SectionError } from '../components/feedback/SectionError'
import { EmptyState, SkeletonRows } from '../components/feedback/States'
import { ConnectionStatus } from '../components/layout/ConnectionStatus'
import { ModelDetail } from '../components/registry/ModelDetail'
import { ModelCardList, ModelTable } from '../components/registry/ModelTable'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardBody, CardHeader } from '../components/ui/Card'
import { Drawer } from '../components/ui/Drawer'
import { KpiCard } from '../components/ui/KpiCard'
import { PageHeader } from '../components/ui/PageHeader'
import { useApiQuery } from '../hooks/useApiQuery'
import { refreshBackendHealth } from '../hooks/useBackendHealth'
import { formatClock, formatInteger } from '../lib/format'
import {
  filterModels, hasActiveFilters, providersOf, sortModels, strategyDescription, summarizeRegistry, tiersOf,
  type RegistryFilters, type StatusFilter,
} from '../lib/registry'

const STATUS_VALUES: readonly StatusFilter[] = ['all', 'enabled', 'disabled']
const FILTER_PARAMS = ['q', 'provider', 'status', 'tier'] as const

function readFilters(params: URLSearchParams): RegistryFilters {
  const status = params.get('status') as StatusFilter | null
  return {
    query: params.get('q') ?? '',
    provider: params.get('provider') ?? '',
    status: status && STATUS_VALUES.includes(status) ? status : 'all',
    tier: params.get('tier') ?? '',
  }
}

const SELECT = 'h-9 w-full rounded-md border border-slate-300 bg-white px-2 text-sm text-slate-700 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500'

/**
 * Model Registry: GET /api/v1/models only (public, read-only; no Redis/PostgreSQL writes).
 * Loaded on open and on Refresh. No polling. Leaving the page aborts an in-flight request.
 */
export function ModelRegistryPage() {
  const query = useApiQuery((signal) => api.models({ signal }))
  const [params, setParams] = useSearchParams()
  const filters = readFilters(params)
  const selectedId = params.get('model')

  const data = query.data
  const all = useMemo(() => data?.models ?? [], [data])
  const summary = useMemo(() => summarizeRegistry(all), [all])
  const providers = useMemo(() => providersOf(all), [all])
  const tiers = useMemo(() => tiersOf(all), [all])
  const visible = sortModels(filterModels(all, filters))
  const selected = selectedId ? all.find((m) => m.id === selectedId) ?? null : null

  const setFilter = (name: (typeof FILTER_PARAMS)[number], value: string) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value === '' || (name === 'status' && value === 'all')) next.delete(name)
      else next.set(name, value)
      return next
    }, { replace: true })
  }
  const clearFilters = () => setParams((prev) => {
    const next = new URLSearchParams(prev)
    for (const p of FILTER_PARAMS) next.delete(p)
    return next
  }, { replace: true })
  const select = useCallback((id: string) => setParams((prev) => {
    const next = new URLSearchParams(prev)
    next.set('model', id)
    return next
  }), [setParams])
  const closeDetail = useCallback(() => setParams((prev) => {
    const next = new URLSearchParams(prev)
    next.delete('model')
    return next
  }, { replace: true }), [setParams])

  const refresh = () => {
    query.reload()
    void refreshBackendHealth()
  }

  const initialLoading = query.loading && !data
  const status = query.error
    ? (data ? 'Refresh failed · showing earlier data' : 'Not loaded')
    : query.updatedAt && data ? `Updated ${formatClock(new Date(query.updatedAt))}` : 'Loading…'
  const filtered = hasActiveFilters(filters)

  return (
    <div className="space-y-6">
      <PageHeader
        title="Model Registry"
        description="Every model the router can use: provider, tier, pricing, limits, routing priority, fallbacks and router health, as configured on the backend."
        actions={(
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span className="hidden sm:inline-flex"><ConnectionStatus /></span>
            <span className="text-xs text-slate-500" aria-live="polite">{status}</span>
            <Button variant="secondary" size="sm" onClick={refresh} loading={query.loading}
              icon={<RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />}>Refresh</Button>
          </div>
        )}
      />

      {/* Summary cards: counts of the API response only */}
      <section aria-labelledby="registry-summary" className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <h2 id="registry-summary" className="text-sm font-semibold text-slate-900">Registry summary</h2>
          <span className="text-xs text-slate-500">GET /api/v1/models</span>
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <KpiCard label="Registered models" icon={<Boxes className="h-4 w-4" />} tone="brand" loading={initialLoading}
            value={data ? formatInteger(summary.total) : '—'}
            hint={data && summary.placeholderPricing > 0 ? `${summary.placeholderPricing} with placeholder pricing` : undefined} />
          <KpiCard label="Enabled" icon={<CheckCircle2 className="h-4 w-4" />} tone="success" loading={initialLoading}
            value={data ? formatInteger(summary.enabled) : '—'} hint={data ? 'Eligible for routing' : undefined} />
          <KpiCard label="Disabled" icon={<XCircle className="h-4 w-4" />} loading={initialLoading}
            value={data ? formatInteger(summary.disabled) : '—'} hint={data ? 'Never routed' : undefined} />
          <KpiCard label="Providers" icon={<Layers className="h-4 w-4" />} loading={initialLoading}
            value={data ? formatInteger(summary.providers.length) : '—'}
            hint={data && summary.providers.length > 0 ? summary.providers.join(', ') : undefined} />
        </div>
      </section>

      {data && (
        <div className="flex flex-wrap items-start gap-x-3 gap-y-2 rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-600 shadow-sm">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />
          <div className="min-w-0 flex-1 space-y-1">
            <p className="flex flex-wrap items-center gap-2">
              <Badge tone={data.mock_mode ? 'info' : 'neutral'}>{data.mock_mode ? 'Mock mode' : 'Real providers'}</Badge>
              <span>Strategy <code className="font-mono text-slate-800">{data.routing_strategy}</code>: {strategyDescription(data.routing_strategy)}</span>
            </p>
            <p>
              <strong className="font-medium text-slate-700">Enabled</strong> means eligible for routing.{' '}
              <strong className="font-medium text-slate-700">Router health</strong> is failure tracking from recent calls
              ({data.health_backend === 'redis' ? 'shared via Redis' : 'this instance only'}), not a live provider check.
              {data.mock_mode && ' In mock mode every call is served by the mock provider; no paid provider is contacted.'}
            </p>
          </div>
        </div>
      )}

      <Card>
        <CardHeader title="Models"
          description={data ? `${visible.length} of ${all.length} shown · select a model for its full registry entry` : 'Registry entries'} />
        {/* Filters */}
        <div className="grid grid-cols-1 gap-3 border-b border-slate-100 px-5 py-4 sm:grid-cols-2 lg:grid-cols-[minmax(0,2fr)_repeat(3,minmax(0,1fr))_auto]"
          role="search" aria-label="Filter models">
          <label className="block min-w-0">
            <span className="mb-1 block text-xs font-medium text-slate-600">Search</span>
            <span className="relative block">
              <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-slate-400" aria-hidden="true" />
              <input type="search" value={filters.query} onChange={(e) => setFilter('q', e.target.value)}
                placeholder="Model ID, provider or provider model" disabled={!data}
                className={`${SELECT} pl-8`} />
            </span>
          </label>
          <label className="block min-w-0">
            <span className="mb-1 block text-xs font-medium text-slate-600">Provider</span>
            <select value={filters.provider} onChange={(e) => setFilter('provider', e.target.value)} className={SELECT} disabled={!data}>
              <option value="">All providers</option>
              {providers.map((p) => <option key={p} value={p}>{p}</option>)}
              {filters.provider && !providers.includes(filters.provider) && <option value={filters.provider}>{filters.provider}</option>}
            </select>
          </label>
          <label className="block min-w-0">
            <span className="mb-1 block text-xs font-medium text-slate-600">Status</span>
            <select value={filters.status} onChange={(e) => setFilter('status', e.target.value)} className={SELECT} disabled={!data}>
              <option value="all">Enabled and disabled</option>
              <option value="enabled">Enabled only</option>
              <option value="disabled">Disabled only</option>
            </select>
          </label>
          <label className="block min-w-0">
            <span className="mb-1 block text-xs font-medium text-slate-600">Tier</span>
            <select value={filters.tier} onChange={(e) => setFilter('tier', e.target.value)} className={SELECT} disabled={!data}>
              <option value="">All tiers</option>
              {tiers.map((t) => <option key={t} value={t}>{t}</option>)}
              {filters.tier && !tiers.includes(filters.tier) && <option value={filters.tier}>{filters.tier}</option>}
            </select>
          </label>
          <div className="flex items-end">
            <Button variant="ghost" size="md" onClick={clearFilters} disabled={!filtered}>Clear filters</Button>
          </div>
        </div>

        {query.error && (
          <div className="px-5 pt-4">
            <SectionError error={query.error} onRetry={refresh} />
          </div>
        )}

        {!data ? (
          query.error ? <div className="h-4" /> : <CardBody><SkeletonRows rows={6} /></CardBody>
        ) : all.length === 0 ? (
          <EmptyState title="No models registered" description="The backend's model registry is empty, so nothing can be routed." />
        ) : visible.length === 0 ? (
          <EmptyState title="No models match these filters" description="Change the search or filters to see registered models."
            action={<Button variant="secondary" size="sm" onClick={clearFilters}>Clear filters</Button>} />
        ) : (
          <>
            <div className="hidden md:block"><ModelTable models={visible} all={all} selectedId={selectedId} onSelect={select} /></div>
            <div className="md:hidden"><ModelCardList models={visible} selectedId={selectedId} onSelect={select} /></div>
          </>
        )}
      </Card>

      {data && (
        <Drawer open={selected !== null} onClose={closeDetail} title={selected?.id ?? ''}
          subtitle={selected ? `${selected.provider} · ${selected.tier} tier` : undefined}>
          {selected && <ModelDetail model={selected} registry={data} onSelect={select} />}
        </Drawer>
      )}
    </div>
  )
}
