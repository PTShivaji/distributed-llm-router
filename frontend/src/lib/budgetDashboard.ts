/**
 * Pure helpers for the Budget Management page (UI only). Validation and loading live in ./budget.ts;
 * nothing here weakens it. Budget days are UTC calendar days, so every date on the page is UTC.
 */

import type { BudgetRefusalsResponse, BudgetStatusResponse, ProcessCounterGroup, RefusalEvent, SpendDay } from '../api/types'
import { MAX_RANGE_DAYS } from './budget'
import { isIsoDate } from './validate'

const DAY_MS = 86_400_000
const dayIndex = (d: string) => Date.parse(`${d}T00:00:00Z`) / DAY_MS

export type BudgetRangeKey = '7d' | '30d' | '90d' | 'custom'

export const BUDGET_RANGE_PRESETS = [
  { key: '7d', label: 'Last 7 days', days: 7 },
  { key: '30d', label: 'Last 30 days', days: 30 },
  { key: '90d', label: 'Last 90 days', days: 90 },
] as const satisfies readonly { key: Exclude<BudgetRangeKey, 'custom'>; label: string; days: number }[]

export const DEFAULT_BUDGET_RANGE: Exclude<BudgetRangeKey, 'custom'> = '30d'

/** Refusal events per page (the backend allows 1..100). */
export const REFUSALS_PAGE_SIZE = 20

export interface BudgetRangeSelection {
  key: BudgetRangeKey
  /** Inclusive UTC days (custom ranges only). */
  start?: string
  end?: string
}

export interface DayRange {
  startDate: string
  endDate: string
}

/** Today's UTC calendar day, YYYY-MM-DD. */
export function utcToday(now: number): string {
  return new Date(now).toISOString().slice(0, 10)
}

export function addDays(day: string, n: number): string {
  return new Date(Date.parse(`${day}T00:00:00Z`) + n * DAY_MS).toISOString().slice(0, 10)
}

/** Inclusive span in days, or null when either date is invalid. */
export function spanDays(start: string, end: string): number | null {
  if (!isIsoDate(start) || !isIsoDate(end)) return null
  return dayIndex(end) - dayIndex(start) + 1
}

/** Why a custom range cannot be applied (mirrors the backend's rules), or null when valid. */
export function customDayRangeError(start: string, end: string, today: string): string | null {
  if (!start || !end) return 'Choose both a start and an end date.'
  const span = spanDays(start, end)
  if (span === null) return 'Dates must be in YYYY-MM-DD format.'
  if (span < 1) return 'The start date must not be after the end date.'
  if (dayIndex(end) > dayIndex(today)) return 'The end date must not be in the future (UTC).'
  if (span > MAX_RANGE_DAYS) return `The range must not exceed ${MAX_RANGE_DAYS} days.`
  return null
}

/** Range from the URL (?range=7d|30d|90d, or ?range=custom&start=&end=). Anything invalid falls back to the default. */
export function readBudgetRange(params: URLSearchParams): BudgetRangeSelection {
  const key = params.get('range')
  if (BUDGET_RANGE_PRESETS.some((p) => p.key === key)) return { key: key as BudgetRangeKey }
  if (key === 'custom') {
    const start = params.get('start') ?? ''
    const end = params.get('end') ?? ''
    const span = spanDays(start, end)
    if (span !== null && span >= 1 && span <= MAX_RANGE_DAYS) return { key: 'custom', start, end }
  }
  return { key: DEFAULT_BUDGET_RANGE }
}

export function writeBudgetRange(prev: URLSearchParams, sel: BudgetRangeSelection): URLSearchParams {
  const next = new URLSearchParams(prev)
  next.delete('start')
  next.delete('end')
  if (sel.key === DEFAULT_BUDGET_RANGE) next.delete('range')
  else next.set('range', sel.key)
  if (sel.key === 'custom' && sel.start && sel.end) {
    next.set('start', sel.start)
    next.set('end', sel.end)
  }
  return next
}

/** Concrete inclusive UTC days for a selection; presets end today (UTC). Both dates are always sent. */
export function resolveBudgetRange(sel: BudgetRangeSelection, now: number): DayRange {
  if (sel.key === 'custom' && sel.start && sel.end) return { startDate: sel.start, endDate: sel.end }
  const preset = BUDGET_RANGE_PRESETS.find((p) => p.key === sel.key) ?? BUDGET_RANGE_PRESETS[1]
  const endDate = utcToday(now)
  return { startDate: addDays(endDate, -(preset.days - 1)), endDate }
}

export function rangeLabel(sel: BudgetRangeSelection): string {
  return sel.key === 'custom' ? 'Custom range' : BUDGET_RANGE_PRESETS.find((p) => p.key === sel.key)!.label
}

// --------------------------------------------------------------------------- overview

/** Share of the global daily limit counted today; null when it cannot be computed (never guessed). */
export function utilization(s: BudgetStatusResponse): number | null {
  const limit = s.limits.daily_usd
  const counted = s.today.counted_usd
  if (limit === null || counted === null || limit <= 0) return null
  return counted / limit
}

export interface EnforcementState {
  label: string
  tone: 'success' | 'neutral' | 'warning'
  detail: string
}

/** Daily enforcement, described only from the backend's flags. */
export function enforcementState(s: BudgetStatusResponse): EnforcementState {
  const e = s.enforcement
  if (!e.configured) return { label: 'Not configured', tone: 'neutral', detail: 'No daily spend limit is configured on the backend.' }
  if (!e.currently_applicable) {
    return { label: 'Configured · inactive', tone: 'neutral',
      detail: 'Limits are configured, but nothing can currently be counted: real provider calls and paid embeddings are disabled.' }
  }
  if (!s.today.available) return { label: 'Active · counters unavailable', tone: 'warning', detail: s.today.unavailable_reason ?? 'Counters unavailable.' }
  return { label: 'Active', tone: 'success', detail: 'Daily limits are checked before each real provider call or paid embedding.' }
}

export const STORE_TEXT: Record<NonNullable<BudgetStatusResponse['enforcement']['store']>, string> = {
  redis: 'Redis',
  memory: 'In-memory',
}

export const STORE_SCOPE_TEXT: Record<NonNullable<BudgetStatusResponse['enforcement']['store_scope']>, string> = {
  shared_across_instances: 'shared across API instances',
  this_process_only: 'this API process only',
}

// --------------------------------------------------------------------------- spend

export interface DailyCostRow {
  date: string
  label: string
  real: number
  mock: number
}

const shortDay = (date: string) =>
  new Date(`${date}T00:00:00Z`).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })

/** One row per day of the range (the backend guarantees consecutive days). Simulated and estimated stay separate. */
export function dailyCostRows(days: SpendDay[]): DailyCostRow[] {
  return days.map((d) => ({ date: d.date, label: shortDay(d.date), real: d.real_estimated_cost_usd, mock: d.mock_estimated_cost_usd }))
}

/** Days that have persisted requests (the table lists only these; the chart shows every day). */
export function activeDays(days: SpendDay[]): SpendDay[] {
  return days.filter((d) => d.requests > 0)
}

// --------------------------------------------------------------------------- refusals

export const REASON_TEXT: Record<string, string> = {
  per_request_limit: 'Per-request cost limit',
  global_daily_budget: 'Global daily budget',
  client_daily_budget: 'Client daily budget',
  client_daily_tokens: 'Client daily token limit',
  budget_unavailable: 'Budget store unavailable',
  other: 'Other',
}

export const reasonText = (key: string) => REASON_TEXT[key] ?? key

export const OUTCOME_TEXT: Record<RefusalEvent['request_outcome'], string> = {
  refused: 'Request refused',
  served_by_another_model: 'Served by another model',
  failed: 'Request failed',
}

export function pageCount(p: BudgetRefusalsResponse['pagination']): number {
  return Math.max(1, Math.ceil(p.total / p.page_size))
}

/** "41–60 of 75" for the current page, or null when the page is empty. */
export function pageWindow(p: BudgetRefusalsResponse['pagination'], shown: number): string | null {
  if (shown === 0) return null
  const first = (p.page - 1) * p.page_size + 1
  return `${first}–${first + shown - 1} of ${p.total}`
}

// --------------------------------------------------------------------------- shared

/** "2026-11-03 10:00 UTC" (budget days are UTC, so event times are shown in UTC too). */
export function formatUtc(iso: string): string {
  const t = Date.parse(iso)
  return Number.isFinite(t) ? `${new Date(t).toISOString().slice(0, 16).replace('T', ' ')} UTC` : iso
}

/** Counter labels as "key=value, …" (sorted by the backend). */
export function counterLabelText(group: ProcessCounterGroup): string {
  const entries = Object.entries(group.labels)
  return entries.length === 0 ? '(no labels)' : entries.map(([k, v]) => `${k}=${v}`).join(', ')
}

/** Backend notes from all loaded responses, de-duplicated, in order. */
export function uniqueNotes(...lists: (readonly string[] | null | undefined)[]): string[] {
  return [...new Set(lists.flatMap((l) => l ?? []))]
}
