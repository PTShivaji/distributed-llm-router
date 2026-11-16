/**
 * Hourly request activity, built ONLY from real aggregates: one GET /api/v1/analytics/summary
 * per UTC hour window ([start, end), the backend's own semantics). The backend has no
 * time-series endpoint, so this is the honest way to chart it. Requests run with bounded
 * concurrency and share one AbortSignal; the caller decides when to refresh (no polling here).
 */

import type { Endpoints } from '../api/endpoints'

export interface HourWindow {
  start: Date
  end: Date
}

export interface HourlyPoint {
  /** ISO start of the window (UTC), stable key */
  start: string
  label: string
  total: number
  successful: number
  failed: number
}

export const HOURS = 24
export const HOURLY_CONCURRENCY = 4

/** The last `hours` UTC hour windows, oldest first; the newest window contains `now`. */
export function hourlyWindows(now: Date = new Date(), hours = HOURS): HourWindow[] {
  const top = new Date(now)
  top.setUTCMinutes(0, 0, 0)
  return Array.from({ length: hours }, (_, i) => {
    const start = new Date(top.getTime() - (hours - 1 - i) * 3_600_000)
    return { start, end: new Date(start.getTime() + 3_600_000) }
  })
}

export function hourLabel(start: Date): string {
  return start.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** Runs `fn` over `items` with at most `limit` in flight; preserves order; stops on abort. */
export async function mapWithConcurrency<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>, signal?: AbortSignal): Promise<R[]> {
  const results = new Array<R>(items.length)
  let next = 0
  async function worker() {
    while (next < items.length) {
      if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
      const index = next++
      results[index] = await fn(items[index])
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker))
  return results
}

export async function fetchHourlyActivity(api: Pick<Endpoints, 'analyticsSummary'>, signal: AbortSignal,
  now: Date = new Date()): Promise<HourlyPoint[]> {
  return mapWithConcurrency(hourlyWindows(now), HOURLY_CONCURRENCY, async (w) => {
    const { data } = await api.analyticsSummary({ start: w.start.toISOString(), end: w.end.toISOString() }, { signal })
    return {
      start: w.start.toISOString(), label: hourLabel(w.start),
      total: data.total_requests, successful: data.successful_requests, failed: data.failed_requests,
    }
  }, signal)
}
