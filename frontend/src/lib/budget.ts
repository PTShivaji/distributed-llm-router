/**
 * GET /api/v1/budget/{status,spend,refusals}: runtime validation, validated loaders and display helpers.
 *
 * Rules carried over from the backend contract (and enforced here, so the UI can rely on them):
 *   - null means "not configured" or "unknown" - it is NEVER shown as 0 / $0.00.
 *   - mock-mode costs are SIMULATED at placeholder prices; real-mode costs are ESTIMATES, never invoices.
 *   - process counters (embeddings, refusals) belong to one API process since its start: not history.
 * Bodies that break the contract are rejected (ApiError code "malformed_response"), never partially used.
 */

import type { ApiResult } from '../api/client'
import type { DateRangeParams, Endpoints, RefusalQueryParams, RequestOptions } from '../api/endpoints'
import { invalidParameters, malformedResponse } from '../api/errors'
import type {
  BudgetRefusalsResponse, BudgetSpendResponse, BudgetStatusResponse, ProcessCounterGroup, RefusalEvent, SpendDay,
  SpendFigures, SpendGroup,
} from '../api/types'
import { formatUsd } from './format'
import {
  arr, bool, count, dict, explain, isIsoDate, isoDate, isoDateTime, lit, nonNeg, nullable, obj, str, type Check,
} from './validate'

export const MAX_RANGE_DAYS = 92
export const MAX_PAGE_SIZE = 100
export const MAX_PAGE = 10_000
export const BUDGET_REASONS = ['per_request_limit', 'global_daily_budget', 'client_daily_budget', 'client_daily_tokens',
  'budget_unavailable', 'other'] as const

const DAY_MS = 86_400_000
const dayIndex = (d: string) => Date.parse(`${d}T00:00:00Z`) / DAY_MS

const counterGroup: Check = obj<ProcessCounterGroup>({ labels: dict(str), value: nonNeg })

// --------------------------------------------------------------------------- status
type St = BudgetStatusResponse

export const budgetStatusCheck: Check = obj<St>({
  period: obj<St['period']>({ timezone: lit('UTC'), granularity: lit('day'), day: isoDate, starts_at: isoDateTime,
    resets_at: isoDateTime }),
  enforcement: obj<St['enforcement']>({
    configured: bool, daily_tracking: bool, applies_to: str, currently_applicable: bool, mock_mode: bool,
    real_provider_calls_enabled: bool, paid_embeddings_enabled: bool, fail_closed: bool,
    store: nullable(lit('redis', 'memory')), store_scope: nullable(lit('shared_across_instances', 'this_process_only')),
  }),
  limits: obj<St['limits']>({ daily_usd: nullable(nonNeg), daily_usd_per_client: nullable(nonNeg),
    daily_tokens_per_client: nullable(count), max_cost_per_request_usd: nullable(nonNeg) }),
  today: obj<St['today']>({
    available: bool, source: lit('budget_counters'), counted_usd: nullable(nonNeg), remaining_usd: nullable(nonNeg),
    unavailable_reason: nullable(str), per_client_counters_exposed: lit(false),
  }, (t, p) => {
    if (t.available !== (t.counted_usd !== null)) return `${p}.available: inconsistent with counted_usd`
    if (!t.available && (typeof t.unavailable_reason !== 'string' || !t.unavailable_reason)) return `${p}.unavailable_reason: required when unavailable`
    if (t.remaining_usd !== null && t.counted_usd === null) return `${p}.remaining_usd: present without counted_usd`
    return null
  }),
  monthly: obj<St['monthly']>({ enforcement_supported: lit(false), limit_usd: lit(null), note: str }),
  embedding: obj<St['embedding']>({
    provider: str, paid: bool, budgeted: bool, persisted_history: lit(false), process_counters_available: bool,
    process_estimated_cost_usd: nullable(nonNeg), process_events: nullable(arr(counterGroup)),
  }, (e, p) => (!e.process_counters_available && (e.process_estimated_cost_usd !== null || e.process_events !== null)
    ? `${p}: process values present although counters are unavailable` : null)),
  notes: arr(str),
}, (o, p) => {
  const limits = o.limits as St['limits']
  const today = o.today as St['today']
  if (today.remaining_usd !== null && limits.daily_usd === null) return `${p}.today.remaining_usd: present without a daily limit`
  return null
})

// --------------------------------------------------------------------------- spend
const figureShape = {
  requests: count, successful_requests: count, total_tokens: count, mock_requests: count, real_requests: count,
  mock_estimated_cost_usd: nonNeg, real_estimated_cost_usd: nonNeg, placeholder_priced_requests: count,
  cost_unknown_requests: count,
}
const figureRules = (f: Record<string, unknown>, p: string) => {
  const n = (k: string) => f[k] as number
  if (n('successful_requests') > n('requests')) return `${p}.successful_requests: exceeds requests`
  if (n('mock_requests') + n('real_requests') !== n('requests')) return `${p}: mock_requests + real_requests != requests`
  return null
}
const figuresCheck: Check = obj<SpendFigures>(figureShape, figureRules)
const dayCheck: Check = obj<SpendDay>({ ...figureShape, date: isoDate }, figureRules)
const groupCheck: Check = obj<SpendGroup>({ name: str, requests: count, total_tokens: count,
  mock_estimated_cost_usd: nonNeg, real_estimated_cost_usd: nonNeg, placeholder_priced_requests: count })

export const budgetSpendCheck: Check = obj<BudgetSpendResponse>({
  start_date: isoDate, end_date: isoDate, timezone: lit('UTC'), source: lit('request_history'), currency: lit('USD'),
  cost_basis: str, totals: figuresCheck, days: arr(dayCheck), by_provider: arr(groupCheck), by_provider_truncated: bool,
  by_model: arr(groupCheck), by_model_truncated: bool, embedding_spend_included: lit(false), notes: arr(str),
}, (o, p) => {
  const start = dayIndex(o.start_date as string)
  const end = dayIndex(o.end_date as string)
  if (start > end) return `${p}.start_date: after end_date`
  if (end - start + 1 > MAX_RANGE_DAYS) return `${p}: range exceeds ${MAX_RANGE_DAYS} days`
  const days = o.days as SpendDay[]
  // One entry per UTC day, consecutive and in order: a gap would otherwise look like "no spend".
  if (days.length !== end - start + 1) return `${p}.days: expected one entry per day of the range`
  for (let i = 0; i < days.length; i++) {
    if (dayIndex(days[i].date) !== start + i) return `${p}.days[${i}].date: not consecutive within the range`
  }
  return null
})

// --------------------------------------------------------------------------- refusals
type Rf = BudgetRefusalsResponse

const eventCheck: Check = obj<RefusalEvent>({
  time: isoDateTime, reason: lit(...BUDGET_REASONS), category: str, description: str, model: nullable(str),
  provider: nullable(str), attempt_number: count, request_outcome: lit('refused', 'served_by_another_model', 'failed'),
  mock_mode: bool,
})
const refusalCount: Check = obj<Rf['by_reason'][number]>({ key: str, count })

export const budgetRefusalsCheck: Check = obj<Rf>({
  start_date: isoDate, end_date: isoDate, timezone: lit('UTC'), source: lit('request_history'), persisted_scope: str,
  total_refused_attempts: count, refused_requests: count, by_reason: arr(refusalCount), by_provider: arr(refusalCount),
  events: arr(eventCheck),
  pagination: obj<Rf['pagination']>({ page: count, page_size: count, total: count, has_more: bool }, (g, p) => {
    if ((g.page as number) < 1) return `${p}.page: must be >= 1`
    if ((g.page_size as number) < 1 || (g.page_size as number) > MAX_PAGE_SIZE) return `${p}.page_size: out of range`
    return null
  }),
  embedding_refusals_persisted: lit(false), process_counters_available: bool,
  process_chat_refusals: nullable(arr(counterGroup)), process_embedding_refusals: nullable(arr(counterGroup)),
  notes: arr(str),
}, (o, p) => {
  const page = o.pagination as Rf['pagination']
  const events = o.events as RefusalEvent[]
  if (dayIndex(o.start_date as string) > dayIndex(o.end_date as string)) return `${p}.start_date: after end_date`
  if (page.total !== o.total_refused_attempts) return `${p}.pagination.total: differs from total_refused_attempts`
  if (events.length > page.page_size) return `${p}.events: more entries than page_size`
  if (!o.process_counters_available && (o.process_chat_refusals !== null || o.process_embedding_refusals !== null)) {
    return `${p}: process counters present although unavailable`
  }
  return null
})

export const isBudgetStatusResponse = (v: unknown): v is BudgetStatusResponse => explain(budgetStatusCheck, v) === null
export const isBudgetSpendResponse = (v: unknown): v is BudgetSpendResponse => explain(budgetSpendCheck, v) === null
export const isBudgetRefusalsResponse = (v: unknown): v is BudgetRefusalsResponse => explain(budgetRefusalsCheck, v) === null

// --------------------------------------------------------------------------- loaders

function validated<T>(endpoint: string, check: Check, result: ApiResult<unknown>): ApiResult<T> {
  const problem = explain(check, result.data)
  if (problem) throw malformedResponse(endpoint, problem, result.meta.status)
  return result as ApiResult<T>
}

/** Client-side checks so obviously invalid filters are never sent (the backend validates again). */
export function checkRange(range: DateRangeParams): void {
  for (const [name, value] of [['startDate', range.startDate], ['endDate', range.endDate]] as const) {
    if (value !== undefined && !isIsoDate(value)) throw invalidParameters(`${name} must be a date in YYYY-MM-DD format.`)
  }
  if (range.startDate !== undefined && range.endDate !== undefined) {
    const span = dayIndex(range.endDate) - dayIndex(range.startDate) + 1
    if (span < 1) throw invalidParameters('startDate must not be after endDate.')
    if (span > MAX_RANGE_DAYS) throw invalidParameters(`The range must not exceed ${MAX_RANGE_DAYS} days.`)
  }
}

export async function loadBudgetStatus(api: Endpoints, o: RequestOptions = {}): Promise<ApiResult<BudgetStatusResponse>> {
  return validated('GET /api/v1/budget/status', budgetStatusCheck, await api.budgetStatus(o))
}

export async function loadBudgetSpend(api: Endpoints, range: DateRangeParams = {}, o: RequestOptions = {}):
  Promise<ApiResult<BudgetSpendResponse>> {
  checkRange(range)
  return validated('GET /api/v1/budget/spend', budgetSpendCheck, await api.budgetSpend(range, o))
}

export async function loadBudgetRefusals(api: Endpoints, query: RefusalQueryParams = {}, o: RequestOptions = {}):
  Promise<ApiResult<BudgetRefusalsResponse>> {
  checkRange(query)
  if (query.page !== undefined && (!Number.isInteger(query.page) || query.page < 1 || query.page > MAX_PAGE)) {
    throw invalidParameters(`page must be an integer from 1 to ${MAX_PAGE}.`)
  }
  if (query.pageSize !== undefined && (!Number.isInteger(query.pageSize) || query.pageSize < 1 || query.pageSize > MAX_PAGE_SIZE)) {
    throw invalidParameters(`pageSize must be an integer from 1 to ${MAX_PAGE_SIZE}.`)
  }
  return validated('GET /api/v1/budget/refusals', budgetRefusalsCheck, await api.budgetRefusals(query, o))
}

// --------------------------------------------------------------------------- display helpers (pure)

export const UNAVAILABLE_TEXT = 'Not available'
export const NOT_CONFIGURED_TEXT = 'Not configured'

/** A configured limit, or "Not configured" (never $0.00). */
export function limitText(value: number | null): string {
  return value === null ? NOT_CONFIGURED_TEXT : formatUsd(value)
}

/** Today's counted spend, or why it is unknown (never $0.00 when unknown). */
export function countedTodayText(status: BudgetStatusResponse): string {
  return status.today.counted_usd === null ? UNAVAILABLE_TEXT : formatUsd(status.today.counted_usd)
}

/** Remaining daily budget: "Not configured" without a global limit, "Not available" when unknown. */
export function remainingTodayText(status: BudgetStatusResponse): string {
  if (status.limits.daily_usd === null) return NOT_CONFIGURED_TEXT
  return status.today.remaining_usd === null ? UNAVAILABLE_TEXT : formatUsd(status.today.remaining_usd)
}

export type CostBasisKind = 'none' | 'simulated' | 'estimated' | 'mixed'

export interface CostBasis {
  kind: CostBasisKind
  label: string
}

/** How persisted cost figures must be described: simulated (mock), estimated (real) - never "billed". */
export function costBasis(f: Pick<SpendFigures, 'requests' | 'mock_requests' | 'real_requests'>): CostBasis {
  if (f.requests === 0) return { kind: 'none', label: 'No persisted requests' }
  if (f.real_requests === 0) return { kind: 'simulated', label: 'Simulated cost (mock mode, placeholder prices; no money spent)' }
  if (f.mock_requests === 0) return { kind: 'estimated', label: 'Estimated cost (not a provider invoice)' }
  return { kind: 'mixed', label: 'Mixed: simulated mock cost and estimated real cost (not invoices)' }
}

/** Process-local counter value, or "Not available" when metrics are disabled (null). */
export function processValueText(value: number | null, fmt: (n: number) => string = formatUsd): string {
  return value === null ? UNAVAILABLE_TEXT : fmt(value)
}
