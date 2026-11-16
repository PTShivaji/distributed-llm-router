/**
 * Observability helpers over GET /metrics (Prometheus text format) and GET /api/v1/health/dependencies.
 *
 * Backend facts (backend/app/observability/metrics.py, backend/app/api/health.py, backend/app/main.py):
 *  - One registry PER PROCESS. Every value is cumulative since that process started, resets on restart,
 *    and covers that instance only. Values from several instances are never added up here.
 *  - Labelled series are created lazily on first use (prometheus_client), so a family can be exposed
 *    (# HELP / # TYPE present) with no samples at all. That is shown as "none recorded", which is not
 *    the same as a sample whose value is 0, and neither is the same as a family that is not exposed.
 *  - llm_router_cache_hit_ratio is NaN until the first hit or miss (undefined, not 0).
 *  - METRICS_ENABLED=false removes the /metrics route entirely (404).
 *  - /health/dependencies returns persistence counters as 0 defaults when no database recorder exists.
 */

import type { DependenciesResponse } from '../api/types'
import { parsePrometheus, type Sample } from './prometheus'

export type MetricType = 'counter' | 'gauge' | 'histogram' | 'summary' | 'untyped'

export interface MetricFamily {
  name: string
  type: MetricType
  help: string
  samples: Sample[]
}

export interface Exposition {
  families: Map<string, MetricFamily>
  /** Non-comment lines that could not be parsed (skipped, never guessed). */
  malformedLines: number
}

const TYPES: readonly MetricType[] = ['counter', 'gauge', 'histogram', 'summary', 'untyped']
const SUFFIXES = ['_bucket', '_sum', '_count'] as const

function family(families: Map<string, MetricFamily>, name: string): MetricFamily {
  let f = families.get(name)
  if (!f) {
    f = { name, type: 'untyped', help: '', samples: [] }
    families.set(name, f)
  }
  return f
}

/** Parses the text format into metric families (HELP/TYPE metadata plus their samples). */
export function parseExposition(text: string): Exposition {
  const families = new Map<string, MetricFamily>()
  let malformedLines = 0
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (line === '') continue
    if (line.startsWith('#')) {
      const meta = /^#\s+(HELP|TYPE)\s+([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\s+(.*))?$/.exec(line)
      if (!meta) continue // other comments
      const f = family(families, meta[2])
      if (meta[1] === 'HELP') f.help = meta[3] ?? ''
      else f.type = (TYPES as readonly string[]).includes(meta[3] ?? '') ? (meta[3] as MetricType) : 'untyped'
      continue
    }
    const [sample] = parsePrometheus(line)
    if (!sample) {
      malformedLines++
      continue
    }
    let owner = families.get(sample.name)
    if (!owner) {
      const suffix = SUFFIXES.find((s) => sample.name.endsWith(s))
      const base = suffix ? families.get(sample.name.slice(0, -suffix.length)) : undefined
      owner = base && (base.type === 'histogram' || base.type === 'summary') ? base : family(families, sample.name)
    }
    owner.samples.push(sample)
  }
  return { families, malformedLines }
}

// --------------------------------------------------------------------------- readings

/** A value as the backend exposes it: not exposed, exposed without samples, or an actual sample value. */
export type Reading =
  | { kind: 'not_exposed' }
  | { kind: 'no_samples' }
  | { kind: 'value'; value: number }

export const NOT_EXPOSED: Reading = { kind: 'not_exposed' }

function matches(s: Sample, where: Record<string, string>): boolean {
  return Object.entries(where).every(([k, v]) => s.labels[k] === v)
}

/** Sum of a counter/gauge family's samples (optionally filtered by labels). */
export function reading(expo: Exposition, name: string, where: Record<string, string> = {}): Reading {
  const f = expo.families.get(name)
  if (!f) return NOT_EXPOSED
  const picked = f.samples.filter((s) => s.name === name && matches(s, where))
  if (picked.length === 0) return { kind: 'no_samples' }
  if (picked.length === 1) return { kind: 'value', value: picked[0].value } // keeps NaN (e.g. undefined ratio)
  return { kind: 'value', value: picked.reduce((n, s) => n + (Number.isFinite(s.value) ? s.value : 0), 0) }
}

export function readingValue(r: Reading): number | null {
  return r.kind === 'value' && Number.isFinite(r.value) ? r.value : null
}

export interface Group {
  labels: Record<string, string>
  value: number
}

/** Samples of `name` summed by the given label keys (zero values kept: they were exposed). Sorted by value desc. */
export function groupBy(expo: Exposition, name: string, keys: readonly string[], where: Record<string, string> = {}): Group[] {
  const f = expo.families.get(name)
  if (!f) return []
  const groups = new Map<string, Group>()
  for (const s of f.samples) {
    if (s.name !== name || !Number.isFinite(s.value) || !matches(s, where)) continue
    const labels = Object.fromEntries(keys.map((k) => [k, s.labels[k] ?? '']))
    const id = JSON.stringify(labels)
    const g = groups.get(id) ?? { labels, value: 0 }
    g.value += s.value
    groups.set(id, g)
  }
  return [...groups.values()].sort((a, b) => b.value - a.value || JSON.stringify(a.labels).localeCompare(JSON.stringify(b.labels)))
}

// --------------------------------------------------------------------------- histograms

export interface Bucket {
  /** Upper bound in the metric's unit (seconds here); Infinity for +Inf. */
  le: number
  /** Cumulative count of observations <= le. */
  cumulative: number
}

export interface HistogramSummary {
  labels: Record<string, string>
  count: number
  sum: number
  buckets: Bucket[]
  /** Buckets are cumulative, non-decreasing and end in +Inf == count. Quantiles are only given when true. */
  consistent: boolean
}

/** Histogram series of `name` summed by the given label keys (all series share the same bucket bounds). */
export function histograms(expo: Exposition, name: string, keys: readonly string[]): HistogramSummary[] {
  const f = expo.families.get(name)
  if (!f || f.type !== 'histogram') return []
  const groups = new Map<string, { labels: Record<string, string>; count: number; sum: number; buckets: Map<number, number> }>()
  for (const s of f.samples) {
    const labels = Object.fromEntries(keys.map((k) => [k, s.labels[k] ?? '']))
    const id = JSON.stringify(labels)
    let g = groups.get(id)
    if (!g) {
      g = { labels, count: 0, sum: 0, buckets: new Map() }
      groups.set(id, g)
    }
    if (s.name === `${name}_count`) g.count += s.value
    else if (s.name === `${name}_sum`) g.sum += s.value
    else if (s.name === `${name}_bucket`) {
      const le = s.labels.le === '+Inf' ? Infinity : Number(s.labels.le)
      if (Number.isNaN(le)) continue
      g.buckets.set(le, (g.buckets.get(le) ?? 0) + s.value)
    }
  }
  return [...groups.values()].map((g) => {
    const buckets = [...g.buckets.entries()].map(([le, cumulative]) => ({ le, cumulative })).sort((a, b) => a.le - b.le)
    const last = buckets[buckets.length - 1]
    const consistent = buckets.length > 0 && last.le === Infinity && last.cumulative === g.count
      && buckets.every((b, i) => i === 0 || b.cumulative >= buckets[i - 1].cumulative)
    return { labels: g.labels, count: g.count, sum: g.sum, buckets, consistent }
  }).sort((a, b) => b.count - a.count || JSON.stringify(a.labels).localeCompare(JSON.stringify(b.labels)))
}

/** Exact mean (sum / count), in the metric's unit; null without observations. */
export function histogramMean(h: HistogramSummary): number | null {
  return h.count > 0 ? h.sum / h.count : null
}

/**
 * The bucket that contains quantile q, as [lower, upper] bounds. This is exact about the BUCKET
 * but says nothing finer: the true quantile lies somewhere inside it. No interpolation is done.
 * upper = Infinity means "above the highest finite bucket bound". Null without observations or
 * when the buckets are inconsistent.
 */
export function quantileBucket(h: HistogramSummary, q: number): { lower: number; upper: number } | null {
  if (!h.consistent || h.count <= 0 || q <= 0 || q > 1) return null
  const target = q * h.count
  for (let i = 0; i < h.buckets.length; i++) {
    if (h.buckets[i].cumulative >= target) return { lower: i === 0 ? 0 : h.buckets[i - 1].le, upper: h.buckets[i].le }
  }
  return null
}

// --------------------------------------------------------------------------- the router's metrics

const P = 'llm_router_'
export const METRIC = {
  build: `${P}build_info`,
  httpRequests: `${P}http_requests_total`,
  httpDuration: `${P}http_request_duration_seconds`,
  httpInProgress: `${P}http_requests_in_progress`,
  errors: `${P}errors_total`,
  chat: `${P}chat_requests_total`,
  attempts: `${P}provider_attempts_total`,
  providerLatency: `${P}provider_latency_seconds`,
  retries: `${P}retries_total`,
  fallbacks: `${P}fallbacks_total`,
  cacheLookups: `${P}cache_lookups_total`,
  cacheHitRatio: `${P}cache_hit_ratio`,
  tokens: `${P}tokens_total`,
  cost: `${P}estimated_cost_usd_total`,
  rateLimited: `${P}rate_limit_rejections_total`,
  budgetRejected: `${P}budget_rejections_total`,
  embeddingEvents: `${P}embedding_budget_events_total`,
  embeddingCost: `${P}embedding_estimated_cost_usd_total`,
} as const

export const SKIPPED_OUTCOMES = ['skipped_unhealthy', 'skipped_rate_limited', 'skipped_budget'] as const

export interface RouteRow {
  method: string
  route: string
  requests: number
  byClass: Record<string, number>
  latency: HistogramSummary | null
}

export interface AttemptRow {
  model: string
  provider: string
  success: number
  error: number
  skipped: Record<(typeof SKIPPED_OUTCOMES)[number], number>
}

export interface MetricsView {
  /** At least one llm_router_* family is present (otherwise this is not the router's /metrics). */
  recognised: boolean
  malformedLines: number
  families: { name: string; type: MetricType; help: string; series: number }[]
  build: { version: string; mockMode: string } | null
  http: { total: Reading; inProgress: Reading; byClass: Group[]; routes: RouteRow[] }
  errors: { total: Reading; rows: Group[] }
  chat: { total: Reading; byOutcome: Group[]; byCache: Group[]; byComplexity: Group[] }
  attempts: { total: Reading; byOutcome: Group[]; rows: AttemptRow[]; errorKinds: Group[]; latency: HistogramSummary[] }
  retries: { total: Reading; rows: Group[] }
  fallbacks: { total: Reading; rows: Group[] }
  cache: { lookups: Reading; byStatus: Group[]; hitRatio: Reading }
  tokens: { input: Reading; output: Reading; bySource: Group[]; byModel: Group[] }
  cost: { total: Reading; rows: Group[]; placeholder: boolean }
  rateLimit: { total: Reading; rows: Group[] }
  budget: { total: Reading; rows: Group[] }
  embedding: { events: Reading; rows: Group[]; cost: Reading }
}

export function metricsView(expo: Exposition): MetricsView {
  const r = (name: string, where?: Record<string, string>) => reading(expo, name, where)
  const g = (name: string, keys: readonly string[], where?: Record<string, string>) => groupBy(expo, name, keys, where)

  const buildSample = expo.families.get(METRIC.build)?.samples[0]
  const latencyByRoute = new Map(histograms(expo, METRIC.httpDuration, ['method', 'route'])
    .map((h) => [`${h.labels.method} ${h.labels.route}`, h]))
  const routes = new Map<string, RouteRow>()
  for (const grp of g(METRIC.httpRequests, ['method', 'route', 'status'])) {
    const { method, route, status } = grp.labels
    const id = `${method} ${route}`
    const row = routes.get(id) ?? { method, route, requests: 0, byClass: {}, latency: latencyByRoute.get(id) ?? null }
    const cls = /^[1-5]\d\d$/.test(status) ? `${status[0]}xx` : 'other'
    row.requests += grp.value
    row.byClass[cls] = (row.byClass[cls] ?? 0) + grp.value
    routes.set(id, row)
  }
  const byClass = new Map<string, number>()
  for (const row of routes.values()) for (const [k, v] of Object.entries(row.byClass)) byClass.set(k, (byClass.get(k) ?? 0) + v)

  const attemptRows = new Map<string, AttemptRow>()
  for (const grp of g(METRIC.attempts, ['model', 'provider', 'outcome'])) {
    const { model, provider, outcome } = grp.labels
    const id = `${model}|${provider}`
    const row = attemptRows.get(id) ?? { model, provider, success: 0, error: 0, skipped: { skipped_unhealthy: 0, skipped_rate_limited: 0, skipped_budget: 0 } }
    if (outcome === 'success') row.success += grp.value
    else if (outcome === 'error') row.error += grp.value
    else if ((SKIPPED_OUTCOMES as readonly string[]).includes(outcome)) row.skipped[outcome as (typeof SKIPPED_OUTCOMES)[number]] += grp.value
    attemptRows.set(id, row)
  }

  return {
    recognised: [...expo.families.keys()].some((n) => n.startsWith(P)),
    malformedLines: expo.malformedLines,
    families: [...expo.families.values()].map((f) => ({ name: f.name, type: f.type, help: f.help,
      series: f.type === 'histogram' ? f.samples.filter((s) => s.name === `${f.name}_count`).length : f.samples.length }))
      .sort((a, b) => a.name.localeCompare(b.name)),
    build: buildSample ? { version: buildSample.labels.version ?? '', mockMode: buildSample.labels.mock_mode ?? '' } : null,
    http: {
      total: r(METRIC.httpRequests),
      inProgress: r(METRIC.httpInProgress),
      byClass: [...byClass.entries()].map(([k, value]) => ({ labels: { class: k }, value })).sort((a, b) => a.labels.class.localeCompare(b.labels.class)),
      routes: [...routes.values()].sort((a, b) => b.requests - a.requests || `${a.route}${a.method}`.localeCompare(`${b.route}${b.method}`)),
    },
    errors: { total: r(METRIC.errors), rows: g(METRIC.errors, ['category', 'code']) },
    chat: {
      total: r(METRIC.chat),
      byOutcome: g(METRIC.chat, ['outcome']),
      byCache: g(METRIC.chat, ['cache_status']),
      byComplexity: g(METRIC.chat, ['complexity']),
    },
    attempts: {
      total: r(METRIC.attempts),
      byOutcome: g(METRIC.attempts, ['outcome']),
      rows: [...attemptRows.values()].sort((a, b) => a.model.localeCompare(b.model) || a.provider.localeCompare(b.provider)),
      errorKinds: g(METRIC.attempts, ['error_kind'], { outcome: 'error' }),
      latency: histograms(expo, METRIC.providerLatency, ['model', 'provider', 'outcome']),
    },
    retries: { total: r(METRIC.retries), rows: g(METRIC.retries, ['model']) },
    fallbacks: { total: r(METRIC.fallbacks), rows: g(METRIC.fallbacks, ['from_model', 'to_model']) },
    cache: { lookups: r(METRIC.cacheLookups), byStatus: g(METRIC.cacheLookups, ['status']), hitRatio: r(METRIC.cacheHitRatio) },
    tokens: {
      input: r(METRIC.tokens, { direction: 'input' }),
      output: r(METRIC.tokens, { direction: 'output' }),
      bySource: g(METRIC.tokens, ['source']),
      byModel: g(METRIC.tokens, ['model', 'direction']),
    },
    cost: {
      total: r(METRIC.cost),
      rows: g(METRIC.cost, ['model', 'pricing']),
      placeholder: (expo.families.get(METRIC.cost)?.samples ?? []).some((s) => s.labels.pricing === 'placeholder'),
    },
    rateLimit: { total: r(METRIC.rateLimited), rows: g(METRIC.rateLimited, ['scope', 'window']) },
    budget: { total: r(METRIC.budgetRejected), rows: g(METRIC.budgetRejected, ['reason']) },
    embedding: { events: r(METRIC.embeddingEvents), rows: g(METRIC.embeddingEvents, ['event', 'reason']), cost: r(METRIC.embeddingCost) },
  }
}

// --------------------------------------------------------------------------- dependency health

export type DependencyState = 'healthy' | 'unavailable' | 'disabled' | 'unknown'
export type OverallState = 'healthy' | 'degraded' | 'unavailable' | 'unknown'
export type Liveness = 'idle' | 'checking' | 'connected' | 'unreachable'

export function dependencyState(d: { enabled: boolean; reachable: boolean } | null | undefined): DependencyState {
  if (!d) return 'unknown'
  if (!d.enabled) return 'disabled'
  return d.reachable ? 'healthy' : 'unavailable'
}

/**
 * Overall status from liveness (GET /health) and a CURRENT dependency read (pass null when the
 * dependency read failed or has not completed: stale dependency data never decides the status).
 */
export function overallState(liveness: Liveness, deps: DependenciesResponse | null): { state: OverallState; reasons: string[] } {
  if (liveness === 'unreachable') return { state: 'unavailable', reasons: ['GET /health did not answer: the API process is unreachable.'] }
  if (liveness !== 'connected') return { state: 'unknown', reasons: ['Liveness has not been confirmed yet.'] }
  if (!deps) return { state: 'unknown', reasons: ['The API is up, but dependency health could not be read.'] }
  const reasons: string[] = []
  if (dependencyState(deps.redis) === 'unavailable') reasons.push('Redis is enabled but not reachable.')
  if (dependencyState(deps.database) === 'unavailable') reasons.push('PostgreSQL is enabled but not reachable.')
  return reasons.length ? { state: 'degraded', reasons } : { state: 'healthy', reasons: [] }
}

function isStatus(v: unknown): v is { enabled: boolean; reachable: boolean } {
  return typeof v === 'object' && v !== null && typeof (v as Record<string, unknown>).enabled === 'boolean'
    && typeof (v as Record<string, unknown>).reachable === 'boolean'
}

/** Runtime shape check: a malformed body is reported as such, never rendered as partial health. */
export function isDependenciesResponse(v: unknown): v is DependenciesResponse {
  if (typeof v !== 'object' || v === null) return false
  const o = v as Record<string, unknown>
  const p = o.persistence
  return isStatus(o.redis) && isStatus(o.database) && typeof o.cache_enabled === 'boolean' && typeof o.rate_limit_enabled === 'boolean'
    && typeof o.health_backend === 'string' && typeof p === 'object' && p !== null
    && Object.values(p).every((n) => typeof n === 'number' && Number.isFinite(n))
}

// --------------------------------------------------------------------------- display

export const NOT_EXPOSED_TEXT = 'Not exposed'
export const NO_SAMPLES_TEXT = 'None recorded'

/** Text for a reading: missing metrics never render as 0; NaN renders as "Undefined". */
export function showReading(r: Reading, fmt: (n: number) => string): string {
  if (r.kind === 'not_exposed') return NOT_EXPOSED_TEXT
  if (r.kind === 'no_samples') return NO_SAMPLES_TEXT
  return Number.isNaN(r.value) ? 'Undefined' : fmt(r.value)
}

/** Seconds -> "4.06 ms" / "1.20 s". */
export function formatSeconds(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '—'
  const ms = seconds * 1000
  if (ms >= 1000) return `${seconds.toFixed(2)} s`
  if (ms >= 10) return `${ms.toFixed(0)} ms`
  return `${ms.toFixed(ms < 1 ? 3 : 2).replace(/\.?0+$/, '')} ms`
}

/** The bucket that holds a quantile, e.g. "10 ms – 25 ms", "≤ 5 ms" or "> 60.00 s". */
export function formatBucketRange(range: { lower: number; upper: number } | null): string {
  if (!range) return '—'
  if (range.upper === Infinity) return `> ${formatSeconds(range.lower)}`
  if (range.lower === 0) return `≤ ${formatSeconds(range.upper)}`
  return `${formatSeconds(range.lower)} – ${formatSeconds(range.upper)}`
}
