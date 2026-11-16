/**
 * Routing Analytics: date ranges and calculations over GET /api/v1/analytics/summary.
 * Every number is either an API field or a plainly labelled arithmetic combination of API
 * fields (shares, averages). Nothing is extrapolated; no time series is invented.
 */

import type { AnalyticsSummaryResponse, ModelsResponse } from '../api/types'

/* ---------------------------------------- date ranges ---------------------------------------- */

export type RangeKey = '1h' | '24h' | '7d' | '30d' | 'all' | 'custom'

const HOUR = 3_600_000

export const RANGE_PRESETS: readonly { key: Exclude<RangeKey, 'custom'>; label: string; short: string; ms: number | null }[] = [
  { key: '1h', label: 'Last hour', short: '1h', ms: HOUR },
  { key: '24h', label: 'Last 24 hours', short: '24h', ms: 24 * HOUR },
  { key: '7d', label: 'Last 7 days', short: '7d', ms: 7 * 24 * HOUR },
  { key: '30d', label: 'Last 30 days', short: '30d', ms: 30 * 24 * HOUR },
  { key: 'all', label: 'All time', short: 'All', ms: null },
]

export const DEFAULT_RANGE: RangeKey = '7d'

export interface RangeSelection {
  key: RangeKey
  /** Only for 'custom': ISO instants (UTC). */
  start?: string
  end?: string
}

export interface ResolvedRange {
  /** Inclusive start; null = no lower bound. */
  start: Date | null
  /** Exclusive end; null = no upper bound. */
  end: Date | null
}

function validIso(value: string | null): string | undefined {
  if (!value) return undefined
  const t = Date.parse(value)
  return Number.isNaN(t) ? undefined : new Date(t).toISOString()
}

/** Reads ?range=…[&start=…&end=…]; anything unknown or invalid falls back to the default. */
export function readRange(params: URLSearchParams): RangeSelection {
  const key = params.get('range')
  if (key === 'custom') {
    const start = validIso(params.get('start'))
    const end = validIso(params.get('end'))
    if (start && end && Date.parse(start) < Date.parse(end)) return { key: 'custom', start, end }
    return { key: DEFAULT_RANGE }
  }
  return RANGE_PRESETS.some((p) => p.key === key) ? { key: key as RangeKey } : { key: DEFAULT_RANGE }
}

export function writeRange(prev: URLSearchParams, sel: RangeSelection): URLSearchParams {
  const next = new URLSearchParams(prev)
  next.delete('start')
  next.delete('end')
  if (sel.key === DEFAULT_RANGE) next.delete('range')
  else next.set('range', sel.key)
  if (sel.key === 'custom' && sel.start && sel.end) {
    next.set('start', sel.start)
    next.set('end', sel.end)
  }
  return next
}

/** Rolling presets end "now" (the moment the request is made); custom ranges are fixed. */
export function resolveRange(sel: RangeSelection, now: number): ResolvedRange {
  if (sel.key === 'custom') return { start: sel.start ? new Date(sel.start) : null, end: sel.end ? new Date(sel.end) : null }
  const preset = RANGE_PRESETS.find((p) => p.key === sel.key)
  if (!preset || preset.ms === null) return { start: null, end: null }
  return { start: new Date(now - preset.ms), end: new Date(now) }
}

/** Query parameters for the analytics endpoint (omitted when unbounded). */
export function rangeQuery(range: ResolvedRange): { start?: string; end?: string } {
  return {
    ...(range.start ? { start: range.start.toISOString() } : {}),
    ...(range.end ? { end: range.end.toISOString() } : {}),
  }
}

const pad = (n: number) => String(n).padStart(2, '0')

/** Date -> value for <input type="datetime-local"> in the browser's local time zone. */
export function toLocalInput(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** <input type="datetime-local"> value (local time) -> Date, or null if empty/invalid. */
export function fromLocalInput(value: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(value)) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

export function customRangeError(start: string, end: string): string | null {
  const s = fromLocalInput(start)
  const e = fromLocalInput(end)
  if (!s || !e) return 'Enter both a start and an end date and time.'
  if (s >= e) return 'The start must be earlier than the end.'
  return null
}

/* ---------------------------------------- calculations ---------------------------------------- */

/** part / whole, or null when whole is 0 (never divides by zero, never invents 0%). */
export function ratio(part: number, whole: number): number | null {
  return whole > 0 ? part / whole : null
}

export const COMPLEXITY_ORDER = ['SIMPLE', 'MEDIUM', 'COMPLEX'] as const

export interface ComplexityRow {
  complexity: string
  requests: number
  share: number | null
}

/** requests_by_complexity in tier order; other keys (e.g. "unknown") after, alphabetically. Empty buckets are not added. */
export function complexityRows(byComplexity: Record<string, number>, total: number): ComplexityRow[] {
  const keys = Object.keys(byComplexity)
  const known = COMPLEXITY_ORDER.filter((k) => keys.includes(k))
  const other = keys.filter((k) => !(COMPLEXITY_ORDER as readonly string[]).includes(k)).sort()
  return [...known, ...other].map((complexity) => ({ complexity, requests: byComplexity[complexity], share: ratio(byComplexity[complexity], total) }))
}

export interface ProviderAttemptRow {
  provider: string
  successes: number
  failures: number
  skipped: number
  total: number
  /** successes / (successes + failures): skipped candidates were never called. null if nothing was called. */
  successRate: number | null
}

export function providerAttemptRows(attempts: AnalyticsSummaryResponse['provider_attempts']): ProviderAttemptRow[] {
  return attempts.map((a) => ({
    ...a, total: a.successes + a.failures + a.skipped, successRate: ratio(a.successes, a.successes + a.failures),
  }))
}

export interface AttemptTotals {
  successes: number
  failures: number
  skipped: number
  total: number
}

export function attemptTotals(rows: readonly ProviderAttemptRow[]): AttemptTotals {
  return rows.reduce((t, r) => ({ successes: t.successes + r.successes, failures: t.failures + r.failures,
    skipped: t.skipped + r.skipped, total: t.total + r.total }), { successes: 0, failures: 0, skipped: 0, total: 0 })
}

export interface UsageSummary {
  inputTokens: number
  outputTokens: number
  totalTokens: number
  estimatedCost: number
  /** total_tokens / total_requests (null without requests). */
  tokensPerRequest: number | null
  /** estimated_total_cost / total_requests (null without requests). */
  costPerRequest: number | null
  /** input share of input + output tokens (null without tokens). */
  inputShare: number | null
}

export function usageSummary(s: AnalyticsSummaryResponse): UsageSummary {
  return {
    inputTokens: s.total_input_tokens,
    outputTokens: s.total_output_tokens,
    totalTokens: s.total_tokens,
    estimatedCost: s.estimated_total_cost,
    tokensPerRequest: ratio(s.total_tokens, s.total_requests),
    costPerRequest: ratio(s.estimated_total_cost, s.total_requests),
    inputShare: ratio(s.total_input_tokens, s.total_input_tokens + s.total_output_tokens),
  }
}

/** True when any model with requests in the range (or the whole registry in mock mode) uses placeholder pricing. */
export function usesPlaceholderPricing(s: AnalyticsSummaryResponse, models: ModelsResponse | null): boolean {
  if (!models) return false
  if (models.mock_mode) return true
  const used = new Set(s.requests_by_model.map((m) => m.model))
  return models.models.some((m) => m.pricing_is_placeholder && used.has(m.id))
}
