import { describe, expect, it } from 'vitest'

import { REAL_METRICS } from '../test/metricsFixture'
import { DEPENDENCIES_FIXTURE } from '../test/server'
import {
  dependencyState, formatBucketRange, formatSeconds, histogramMean, histograms, isDependenciesResponse, METRIC, metricsView, overallState,
  parseExposition, quantileBucket, reading, showReading,
} from './observability'

const int = (n: number) => String(n)

describe('parseExposition', () => {
  const expo = parseExposition(REAL_METRICS)

  it('reads HELP/TYPE metadata and assigns histogram _bucket/_sum/_count samples to their family', () => {
    expect(expo.families.get(METRIC.httpRequests)?.type).toBe('counter')
    expect(expo.families.get(METRIC.httpInProgress)?.type).toBe('gauge')
    const duration = expo.families.get(METRIC.httpDuration)!
    expect(duration.type).toBe('histogram')
    expect(duration.help).toBe('HTTP request latency.')
    expect(new Set(duration.samples.map((s) => s.name))).toEqual(new Set([`${METRIC.httpDuration}_bucket`, `${METRIC.httpDuration}_sum`, `${METRIC.httpDuration}_count`]))
    expect(expo.families.has(`${METRIC.httpDuration}_bucket`)).toBe(false)
    expect(expo.malformedLines).toBe(0)
  })

  it('keeps families that are exposed without samples (lazily created labelled series)', () => {
    expect(expo.families.get(METRIC.rateLimited)?.samples).toEqual([])
    expect(expo.families.get(METRIC.budgetRejected)?.samples).toEqual([])
  })

  it('counts malformed lines instead of guessing values', () => {
    const e = parseExposition('# TYPE x_total counter\nx_total{a="1"} 2\nx_total{a="1" 3\nnot a sample line\n')
    expect(e.malformedLines).toBe(2)
    expect(reading(e, 'x_total')).toEqual({ kind: 'value', value: 2 })
  })
})

describe('readings: missing is not zero', () => {
  const expo = parseExposition(REAL_METRICS)

  it('distinguishes not exposed, exposed without samples, a real 0 and a real value', () => {
    expect(reading(expo, 'llm_router_does_not_exist_total')).toEqual({ kind: 'not_exposed' })
    expect(reading(expo, METRIC.rateLimited)).toEqual({ kind: 'no_samples' })
    expect(reading(expo, METRIC.embeddingCost)).toEqual({ kind: 'value', value: 0 }) // unlabelled counter: an actual 0
    expect(reading(expo, METRIC.httpRequests)).toEqual({ kind: 'value', value: 16 })
    expect(showReading({ kind: 'not_exposed' }, int)).toBe('Not exposed')
    expect(showReading({ kind: 'no_samples' }, int)).toBe('None recorded')
    expect(showReading({ kind: 'value', value: 0 }, int)).toBe('0')
    expect(showReading({ kind: 'value', value: NaN }, int)).toBe('Undefined')
  })

  it('keeps the NaN hit ratio (undefined before any lookup) rather than turning it into 0', () => {
    const e = parseExposition('# TYPE llm_router_cache_hit_ratio gauge\nllm_router_cache_hit_ratio NaN\n')
    const r = reading(e, METRIC.cacheHitRatio)
    expect(r.kind === 'value' && Number.isNaN(r.value)).toBe(true)
  })
})

describe('metricsView over the real backend output', () => {
  const view = metricsView(parseExposition(REAL_METRICS))

  it('recognises the router and reads build info', () => {
    expect(view.recognised).toBe(true)
    expect(view.build).toEqual({ version: '0.1.0', mockMode: 'true' })
    expect(metricsView(parseExposition('# TYPE up gauge\nup 1\n')).recognised).toBe(false)
  })

  it('HTTP: totals per route and per status class, gauge in flight', () => {
    expect(view.http.inProgress).toEqual({ kind: 'value', value: 1 })
    expect(Object.fromEntries(view.http.byClass.map((g) => [g.labels.class, g.value]))).toEqual({ '2xx': 8, '4xx': 8 })
    const chat = view.http.routes.find((r) => r.route === '/api/v1/chat' && r.method === 'POST')!
    expect(chat.requests).toBe(11)
    expect(chat.byClass).toEqual({ '2xx': 4, '4xx': 7 })
    expect(chat.latency?.count).toBe(11)
  })

  it('errors by category and code', () => {
    expect(view.errors.total).toEqual({ kind: 'value', value: 8 })
    expect(view.errors.rows.map((g) => [g.labels.category, g.labels.code, g.value])).toEqual([
      ['validation', 'validation_error', 6], ['auth', 'unauthorized', 1], ['client', 'unknown_model', 1],
    ])
  })

  it('provider attempts distinguish success, error and skipped outcomes; latency only for invoked attempts', () => {
    expect(view.attempts.total).toEqual({ kind: 'value', value: 5 })
    const adv = view.attempts.rows.find((r) => r.model === 'mock-advanced')!
    expect(adv).toEqual({ model: 'mock-advanced', provider: 'mock', success: 0, error: 2,
      skipped: { skipped_unhealthy: 0, skipped_rate_limited: 0, skipped_budget: 0 } })
    expect(view.attempts.errorKinds.map((g) => [g.labels.error_kind, g.value])).toEqual([['unavailable', 2]])
    expect(view.attempts.latency.map((h) => [h.labels.model, h.labels.outcome, h.count])).toEqual(expect.arrayContaining([
      ['mock-advanced', 'error', 2], ['mock-fast', 'success', 2], ['mock-balanced', 'success', 1]]))
  })

  it('counts skipped attempts separately from errors', () => {
    const v = metricsView(parseExposition([
      '# TYPE llm_router_provider_attempts_total counter',
      'llm_router_provider_attempts_total{error_kind="none",model="m",outcome="skipped_unhealthy",provider="p"} 3',
      'llm_router_provider_attempts_total{error_kind="none",model="m",outcome="skipped_budget",provider="p"} 1',
      'llm_router_provider_attempts_total{error_kind="timeout",model="m",outcome="error",provider="p"} 2',
    ].join('\n')))
    expect(v.attempts.rows[0]).toMatchObject({ error: 2, success: 0, skipped: { skipped_unhealthy: 3, skipped_rate_limited: 0, skipped_budget: 1 } })
    expect(v.attempts.errorKinds.map((g) => g.labels.error_kind)).toEqual(['timeout'])
  })

  it('retries, fallbacks, cache, tokens, cost (placeholder pricing) and safeguards', () => {
    expect(view.retries.total).toEqual({ kind: 'value', value: 1 })
    expect(view.fallbacks.rows.map((g) => [g.labels.from_model, g.labels.to_model, g.value])).toEqual([['mock-advanced', 'mock-balanced', 1]])
    expect(view.cache.hitRatio).toEqual({ kind: 'value', value: 0.25 })
    expect(view.cache.byStatus.map((g) => [g.labels.status, g.value])).toEqual([['miss', 3], ['hit_exact', 1]])
    expect(view.tokens.input).toEqual({ kind: 'value', value: 53 })
    expect(view.tokens.output).toEqual({ kind: 'value', value: 151 })
    expect(view.cost.placeholder).toBe(true)
    expect(view.cost.total.kind === 'value' && view.cost.total.value).toBeCloseTo(0.0002406, 10)
    expect(view.rateLimit.total).toEqual({ kind: 'no_samples' })
    expect(view.budget.total).toEqual({ kind: 'no_samples' })
    expect(view.embedding.events).toEqual({ kind: 'no_samples' })
    expect(view.embedding.cost).toEqual({ kind: 'value', value: 0 })
  })

  it('lists every exposed family with its type and series count', () => {
    const fam = Object.fromEntries(view.families.map((f) => [f.name, f]))
    expect(fam[METRIC.httpDuration]).toMatchObject({ type: 'histogram', series: 5 })
    expect(fam[METRIC.rateLimited]).toMatchObject({ type: 'counter', series: 0 })
  })
})

describe('histograms', () => {
  const expo = parseExposition(REAL_METRICS)
  const [chat] = histograms(expo, METRIC.httpDuration, ['method', 'route']).filter((h) => h.labels.route === '/api/v1/chat')

  it('reads cumulative buckets (seconds) ending in +Inf == count', () => {
    expect(chat.consistent).toBe(true)
    expect(chat.buckets.at(-1)).toEqual({ le: Infinity, cumulative: 11 })
    expect(chat.buckets[0]).toEqual({ le: 0.001, cumulative: 0 })
    expect(histogramMean(chat)).toBeCloseTo(0.5835397000046214 / 11, 12)
  })

  it('places quantiles in a bucket, without interpolation', () => {
    // cumulative: <=5ms 3, <=10ms 4, <=25ms 8, <=100ms 10, <=500ms 11
    expect(quantileBucket(chat, 0.5)).toEqual({ lower: 0.01, upper: 0.025 })
    expect(quantileBucket(chat, 0.95)).toEqual({ lower: 0.25, upper: 0.5 })
    expect(quantileBucket(chat, 0.2)).toEqual({ lower: 0.0025, upper: 0.005 })
    expect(formatBucketRange(quantileBucket(chat, 0.5))).toBe('10 ms – 25 ms')
    expect(formatBucketRange({ lower: 0, upper: 0.005 })).toBe('≤ 5 ms')
    expect(formatBucketRange({ lower: 60, upper: Infinity })).toBe('> 60.00 s')
  })

  it('gives no quantile without observations or with inconsistent buckets', () => {
    const e = parseExposition([
      '# TYPE h_seconds histogram',
      'h_seconds_bucket{le="0.1"} 5', 'h_seconds_bucket{le="1"} 3', 'h_seconds_bucket{le="+Inf"} 5', 'h_seconds_count 5', 'h_seconds_sum 1',
    ].join('\n'))
    const [h] = histograms(e, 'h_seconds', [])
    expect(h.consistent).toBe(false)
    expect(quantileBucket(h, 0.5)).toBeNull()
    expect(quantileBucket({ ...chat, count: 0 }, 0.5)).toBeNull()
  })

  it('formats seconds with explicit units', () => {
    expect(formatSeconds(0.0053)).toBe('5.3 ms')
    expect(formatSeconds(0.025)).toBe('25 ms')
    expect(formatSeconds(0.0005)).toBe('0.5 ms')
    expect(formatSeconds(2.5)).toBe('2.50 s')
    expect(formatSeconds(null)).toBe('—')
  })
})

describe('dependency health', () => {
  it('maps enabled/reachable to healthy, unavailable, disabled and unknown', () => {
    expect(dependencyState({ enabled: true, reachable: true })).toBe('healthy')
    expect(dependencyState({ enabled: true, reachable: false })).toBe('unavailable')
    expect(dependencyState({ enabled: false, reachable: false })).toBe('disabled')
    expect(dependencyState(null)).toBe('unknown')
  })

  it('overall: healthy, degraded (enabled dependency down), unavailable (API down), unknown (no current read)', () => {
    expect(overallState('connected', DEPENDENCIES_FIXTURE).state).toBe('healthy')
    const dbDown = { ...DEPENDENCIES_FIXTURE, database: { enabled: true, reachable: false } }
    expect(overallState('connected', dbDown)).toEqual({ state: 'degraded', reasons: ['PostgreSQL is enabled but not reachable.'] })
    const disabled = { ...DEPENDENCIES_FIXTURE, database: { enabled: false, reachable: false } }
    expect(overallState('connected', disabled).state).toBe('healthy') // a disabled dependency is not a failure
    expect(overallState('unreachable', DEPENDENCIES_FIXTURE).state).toBe('unavailable')
    expect(overallState('connected', null).state).toBe('unknown')
    expect(overallState('checking', DEPENDENCIES_FIXTURE).state).toBe('unknown')
  })

  it('rejects malformed dependency bodies', () => {
    expect(isDependenciesResponse(DEPENDENCIES_FIXTURE)).toBe(true)
    expect(isDependenciesResponse({ ...DEPENDENCIES_FIXTURE, redis: { enabled: 'yes', reachable: true } })).toBe(false)
    expect(isDependenciesResponse({ ...DEPENDENCIES_FIXTURE, persistence: { written: 'x' } })).toBe(false)
    expect(isDependenciesResponse('<html>')).toBe(false)
    expect(isDependenciesResponse(null)).toBe(false)
  })
})
