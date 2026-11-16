import { useCallback, useState } from 'react'
import { useSearchParams } from 'react-router'
import { Info, RefreshCw } from 'lucide-react'

import { api } from '../api/endpoints'
import type { ApiError } from '../api/errors'
import type { BudgetRefusalsResponse, BudgetSpendResponse, BudgetStatusResponse } from '../api/types'
import {
  BudgetOverviewSection, DailySpendSection, EmbeddingSection, LimitationsSection, RefusalsSection, SpendBreakdownSection,
} from '../components/budget/BudgetSections'
import { DayRangeFilter } from '../components/budget/DayRangeFilter'
import { ConnectionStatus } from '../components/layout/ConnectionStatus'
import { SectionFailure } from '../components/observability/StaleNotice'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardBody } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { useApiQuery } from '../hooks/useApiQuery'
import { refreshBackendHealth } from '../hooks/useBackendHealth'
import { loadBudgetRefusals, loadBudgetSpend, loadBudgetStatus } from '../lib/budget'
import {
  rangeLabel, readBudgetRange, REFUSALS_PAGE_SIZE, resolveBudgetRange, utcToday, writeBudgetRange, type BudgetRangeSelection,
} from '../lib/budgetDashboard'
import { formatClock } from '../lib/format'

interface Loaded<T> {
  value: T
  at: number
  /** The range (and page) this result belongs to, so a failed change never shows another range's data as current. */
  rangeId: string
  page: number
}

/**
 * Budget Management (read-only). Three validated loaders (src/lib/budget.ts), all with the observability token:
 *   GET /api/v1/budget/status    limits, today's counted spend, period (not ranged)
 *   GET /api/v1/budget/spend     persisted cost estimates per UTC day / provider / model (ranged)
 *   GET /api/v1/budget/refusals  persisted budget refusals (ranged, paginated)
 * Loaded on open, on range or page change and with Refresh only: never polled, never retried automatically.
 * A failed refresh keeps the earlier data of the same range, marked stale. Nothing here changes the backend.
 */
export function BudgetManagementPage() {
  const [params, setParams] = useSearchParams()
  const selection = readBudgetRange(params)
  const rangeId = JSON.stringify(selection)
  const [refreshKey, setRefreshKey] = useState(0)
  const [mountDay] = useState(() => utcToday(Date.now()))
  const [paging, setPaging] = useState({ rangeId, page: 1 })
  const page = paging.rangeId === rangeId ? paging.page : 1

  const status = useApiQuery<Loaded<BudgetStatusResponse>>(async (signal) => {
    const { data, meta } = await loadBudgetStatus(api, { signal })
    return { data: { value: data, at: Date.now(), rangeId: '', page: 0 }, meta }
  }, refreshKey)

  const spend = useApiQuery<Loaded<BudgetSpendResponse>>(async (signal) => {
    const { data, meta } = await loadBudgetSpend(api, resolveBudgetRange(selection, Date.now()), { signal })
    return { data: { value: data, at: Date.now(), rangeId, page: 0 }, meta }
  }, `${rangeId}#${refreshKey}`)

  const refusals = useApiQuery<Loaded<BudgetRefusalsResponse>>(async (signal) => {
    const range = resolveBudgetRange(selection, Date.now())
    const { data, meta } = await loadBudgetRefusals(api, { ...range, page, pageSize: REFUSALS_PAGE_SIZE }, { signal })
    return { data: { value: data, at: Date.now(), rangeId, page }, meta }
  }, `${rangeId}#${page}#${refreshKey}`)

  const setRange = useCallback((sel: BudgetRangeSelection) => setParams((prev) => writeBudgetRange(prev, sel), { replace: true }), [setParams])
  const setPage = (p: number) => setPaging({ rangeId, page: p })
  const refresh = () => {
    setRefreshKey((k) => k + 1)
    void refreshBackendHealth()
  }

  const st = status.data?.value ?? null
  const sp = spend.data?.rangeId === rangeId ? spend.data.value : null
  const rfExact = refusals.data?.rangeId === rangeId && refusals.data.page === page
  // While another page of the same range loads, the current rows stay visible (controls are disabled).
  const rf = rfExact || (refusals.data?.rangeId === rangeId && refusals.loading) ? refusals.data!.value : null

  const loading = status.loading || spend.loading || refusals.loading
  const failures: [ApiError | null, unknown][] = [[status.error, st], [spend.error, sp], [refusals.error, rfExact ? rf : null]]
  const failed = failures.filter(([e]) => e)
  const latest = Math.max(status.data?.at ?? 0, sp ? spend.data!.at : 0, rf ? refusals.data!.at : 0)
  const headerStatus = loading ? 'Loading…'
    : failed.length === 0 ? (latest ? `Updated ${formatClock(new Date(latest))}` : 'Loading…')
      : failed.every(([, v]) => v !== null) ? 'Refresh failed · showing earlier data'
        : failed.length === failures.length ? 'Not loaded' : 'Some sections failed to load'

  // One token prompt per page: Budget overview owns it, then Daily spending.
  const statusUnauthorized = status.error?.kind === 'unauthorized'
  const spendUnauthorized = spend.error?.kind === 'unauthorized'
  const elsewhere = (section: string) => `Enter it in the ${section} section above; every section reloads with it.`
  // Latest allowed custom end day: the server's budget day when known (authoritative UTC "today"), else the browser's.
  const today = st?.period.day ?? mountDay

  return (
    <div className="space-y-6" data-testid="budget-page">
      <PageHeader
        title="Budget Management"
        description="Read-only view of the backend's daily budget, estimated spend from persisted requests, and budget refusals."
        actions={(
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span className="hidden sm:inline-flex"><ConnectionStatus /></span>
            <Badge tone="neutral">Read-only</Badge>
            <span className="text-xs text-slate-500" aria-live="polite" data-testid="refresh-status">{headerStatus}</span>
            <Button variant="secondary" size="sm" onClick={refresh} loading={loading}
              icon={<RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />}>Refresh</Button>
          </div>
        )}
      />

      <p className="flex gap-2 rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-600 shadow-sm" role="note" data-testid="budget-note">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />
        <span>
          <strong className="font-medium text-slate-700">Nothing on this page changes the backend.</strong> Limits come from environment
          variables on the server. Amounts are in USD and are estimates, never provider invoices; budget days are UTC calendar days.
          With several API instances, the one that answered is shown.
        </span>
      </p>

      <BudgetOverviewSection status={st} loading={status.loading}
        failure={status.error && <SectionFailure error={status.error} hasData={st !== null} at={status.data?.at ?? null}
          what="budget status" onRetry={refresh} />} />

      <Card>
        <CardBody className="space-y-3">
          <DayRangeFilter key={selection.key === 'custom' ? rangeId : 'preset'} selection={selection} onChange={setRange} today={today} />
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-500" data-testid="range-window">
            <Badge tone="info">{rangeLabel(selection)}</Badge>
            <span>{sp ? `${sp.start_date} → ${sp.end_date} · UTC days, inclusive` : spend.error ? 'Range not loaded' : 'Loading…'}</span>
            <span className="text-slate-400">· applies to Daily spending, Spending breakdown and Budget refusals</span>
          </p>
        </CardBody>
      </Card>

      <DailySpendSection spend={sp} loading={spend.loading}
        failure={spend.error && <SectionFailure error={spend.error} hasData={sp !== null} at={sp ? spend.data!.at : null}
          what="spending" onRetry={refresh} prompt={!statusUnauthorized} elsewhere={elsewhere('Budget overview')} />} />

      {sp && <SpendBreakdownSection spend={sp} />}

      <RefusalsSection refusals={rf} loading={refusals.loading} onPage={setPage}
        failure={refusals.error && <SectionFailure error={refusals.error} hasData={rfExact && rf !== null}
          at={rfExact ? refusals.data!.at : null} what="refusals" onRetry={refresh}
          prompt={!statusUnauthorized && !spendUnauthorized}
          elsewhere={elsewhere(statusUnauthorized ? 'Budget overview' : 'Daily spending')} />} />

      <EmbeddingSection status={st} refusals={rf} />

      <LimitationsSection status={st} spend={sp} refusals={rf} />
    </div>
  )
}
