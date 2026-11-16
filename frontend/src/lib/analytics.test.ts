import { describe, expect, it } from 'vitest'

import type { AnalyticsSummaryResponse } from '../api/types'
import { EMPTY_SUMMARY, MODELS_FIXTURE, modelFixture } from '../test/server'
import {
  attemptTotals, complexityRows, customRangeError, DEFAULT_RANGE, fromLocalInput, providerAttemptRows, rangeQuery, ratio, readRange,
  resolveRange, toLocalInput, usageSummary, usesPlaceholderPricing, writeRange,
} from './analytics'
import { parsePrometheus, routerCounters, sumBy, total } from './prometheus'

const NOW = Date.parse('2026-11-09T12:00:00Z')

describe('date ranges', () => {
  it('resolves rolling presets relative to now, and all-time as unbounded', () => {
    expect(rangeQuery(resolveRange({ key: '24h' }, NOW))).toEqual({ start: '2026-11-08T12:00:00.000Z', end: '2026-11-09T12:00:00.000Z' })
    expect(rangeQuery(resolveRange({ key: '1h' }, NOW))).toEqual({ start: '2026-11-09T11:00:00.000Z', end: '2026-11-09T12:00:00.000Z' })
    expect(rangeQuery(resolveRange({ key: '30d' }, NOW)).start).toBe('2026-10-10T12:00:00.000Z')
    expect(rangeQuery(resolveRange({ key: 'all' }, NOW))).toEqual({})
    expect(rangeQuery(resolveRange({ key: 'custom', start: '2026-11-01T00:00:00.000Z', end: '2026-11-02T00:00:00.000Z' }, NOW)))
      .toEqual({ start: '2026-11-01T00:00:00.000Z', end: '2026-11-02T00:00:00.000Z' })
  })

  it('reads and writes URL params, rejecting invalid or inverted custom ranges', () => {
    const p = (s: string) => new URLSearchParams(s)
    expect(readRange(p(''))).toEqual({ key: DEFAULT_RANGE })
    expect(readRange(p('range=24h'))).toEqual({ key: '24h' })
    expect(readRange(p('range=bogus'))).toEqual({ key: DEFAULT_RANGE })
    expect(readRange(p('range=custom&start=2026-11-01T00:00:00Z&end=2026-11-02T00:00:00Z')))
      .toEqual({ key: 'custom', start: '2026-11-01T00:00:00.000Z', end: '2026-11-02T00:00:00.000Z' })
    expect(readRange(p('range=custom&start=2026-11-02T00:00:00Z&end=2026-11-01T00:00:00Z'))).toEqual({ key: DEFAULT_RANGE })
    expect(readRange(p('range=custom&start=garbage&end=2026-11-01T00:00:00Z'))).toEqual({ key: DEFAULT_RANGE })
    expect(writeRange(p('other=1&range=24h'), { key: DEFAULT_RANGE }).toString()).toBe('other=1')
    expect(writeRange(p('start=x&end=y'), { key: 'all' }).toString()).toBe('range=all')
    expect(writeRange(p(''), { key: 'custom', start: 'a', end: 'b' }).toString()).toBe('range=custom&start=a&end=b')
  })

  it('round-trips datetime-local values and validates custom input', () => {
    const d = new Date(2026, 10, 9, 8, 5)
    expect(toLocalInput(d)).toBe('2026-11-09T08:05')
    expect(fromLocalInput('2026-11-09T08:05')!.getTime()).toBe(d.getTime())
    expect(fromLocalInput('')).toBeNull()
    expect(fromLocalInput('not-a-date')).toBeNull()
    expect(customRangeError('', '2026-11-09T08:05')).toMatch(/both/)
    expect(customRangeError('2026-11-09T09:00', '2026-11-09T08:00')).toMatch(/earlier/)
    expect(customRangeError('2026-11-09T08:00', '2026-11-09T08:00')).toMatch(/earlier/)
    expect(customRangeError('2026-11-09T08:00', '2026-11-09T09:00')).toBeNull()
  })
})

describe('analytics calculations', () => {
  it('ratio never divides by zero', () => {
    expect(ratio(1, 4)).toBe(0.25)
    expect(ratio(0, 0)).toBeNull()
  })

  it('orders complexity buckets by tier and keeps other keys, with shares', () => {
    expect(complexityRows({ unknown: 1, COMPLEX: 5, SIMPLE: 10, MEDIUM: 4 }, 20)).toEqual([
      { complexity: 'SIMPLE', requests: 10, share: 0.5 }, { complexity: 'MEDIUM', requests: 4, share: 0.2 },
      { complexity: 'COMPLEX', requests: 5, share: 0.25 }, { complexity: 'unknown', requests: 1, share: 0.05 },
    ])
    expect(complexityRows({ MEDIUM: 2 }, 2)).toEqual([{ complexity: 'MEDIUM', requests: 2, share: 1 }]) // no invented empty tiers
    expect(complexityRows({}, 0)).toEqual([])
  })

  it('computes provider attempt totals and call success (skipped excluded)', () => {
    const rows = providerAttemptRows([
      { provider: 'mock', successes: 18, failures: 2, skipped: 3 },
      { provider: 'openai', successes: 0, failures: 0, skipped: 4 },
    ])
    expect(rows[0]).toMatchObject({ total: 23, successRate: 0.9 })
    expect(rows[1]).toMatchObject({ total: 4, successRate: null })
    expect(attemptTotals(rows)).toEqual({ successes: 18, failures: 2, skipped: 7, total: 27 })
  })

  it('summarizes usage with per-request averages, null when there are no requests or tokens', () => {
    const s: AnalyticsSummaryResponse = { ...EMPTY_SUMMARY, total_requests: 4, total_input_tokens: 300, total_output_tokens: 100, total_tokens: 400,
      estimated_total_cost: 0.02 }
    expect(usageSummary(s)).toEqual({ inputTokens: 300, outputTokens: 100, totalTokens: 400, estimatedCost: 0.02, tokensPerRequest: 100,
      costPerRequest: 0.005, inputShare: 0.75 })
    expect(usageSummary(EMPTY_SUMMARY)).toMatchObject({ tokensPerRequest: null, costPerRequest: null, inputShare: null })
  })

  it('flags placeholder pricing only for models used in the range (or mock mode)', () => {
    const s = { ...EMPTY_SUMMARY, requests_by_model: [{ model: 'real', requests: 1, total_tokens: 1, estimated_cost: 0, average_latency_ms: 1 }] }
    const registry = { ...MODELS_FIXTURE, mock_mode: false, models: [modelFixture('real', { pricing_is_placeholder: false }), modelFixture('ph')] }
    expect(usesPlaceholderPricing(s, registry)).toBe(false)
    expect(usesPlaceholderPricing({ ...s, requests_by_model: [{ ...s.requests_by_model[0], model: 'ph' }] }, registry)).toBe(true)
    expect(usesPlaceholderPricing(s, { ...registry, mock_mode: true })).toBe(true)
    expect(usesPlaceholderPricing(s, null)).toBe(false)
  })
})

const METRICS = String.raw`# HELP llm_router_retries_total Retries of the same model after a transient error.
# TYPE llm_router_retries_total counter
llm_router_retries_total{model="mock-advanced"} 4.0
llm_router_retries_created{model="mock-advanced"} 1.7627e+09
llm_router_retries_total{model="mock-fast"} 1.0
# TYPE llm_router_fallbacks_total counter
llm_router_fallbacks_total{from_model="mock-advanced",to_model="mock-balanced"} 3.0
llm_router_fallbacks_total{from_model="mock-fast",to_model="mock-balanced"} 0.0
llm_router_provider_attempts_total{error_kind="unavailable",model="mock-advanced",outcome="error",provider="mock"} 6.0
llm_router_provider_attempts_total{error_kind="timeout",model="mock-fast",outcome="error",provider="mock"} 1.0
llm_router_provider_attempts_total{error_kind="",model="mock-fast",outcome="success",provider="mock"} 9.0
llm_router_chat_requests_total{cache_status="miss",complexity="SIMPLE",model_used="mock-fast",outcome="success",selected_model="mock-fast"} 7.0
llm_router_chat_requests_total{cache_status="hit_exact",complexity="SIMPLE",model_used="mock-fast",outcome="success",selected_model="mock-fast"} 2.0
llm_router_cache_hit_ratio NaN
weird_label{msg="a \"quoted\" \\ value\nnext"} 1
broken{unterminated="x} 1
llm_router_http_request_duration_seconds_bucket{le="+Inf",method="GET",route="/health"} 3.0
`

describe('Prometheus parsing', () => {
  const samples = parsePrometheus(METRICS)

  it('parses samples, escapes and special values, skipping comments and malformed lines', () => {
    expect(samples.find((s) => s.name === 'weird_label')?.labels.msg).toBe('a "quoted" \\ value\nnext')
    expect(samples.some((s) => s.name === 'broken')).toBe(false)
    expect(Number.isNaN(samples.find((s) => s.name === 'llm_router_cache_hit_ratio')!.value)).toBe(true)
    expect(samples.find((s) => s.name.endsWith('_bucket'))?.labels.le).toBe('+Inf')
    expect(parsePrometheus('')).toEqual([])
  })

  it('sums by label and totals with filters', () => {
    expect(sumBy(samples, 'llm_router_retries_total', ['model'])).toEqual([
      { labels: { model: 'mock-advanced' }, value: 4 }, { labels: { model: 'mock-fast' }, value: 1 },
    ])
    expect(total(samples, 'llm_router_chat_requests_total')).toBe(9)
    expect(total(samples, 'llm_router_chat_requests_total', { cache_status: 'hit_exact' })).toBe(2)
  })

  it('extracts retries, fallbacks (zero pairs dropped) and failed attempts by error kind', () => {
    expect(routerCounters(samples)).toEqual({
      recognised: true,
      chatRequests: 9,
      retries: [{ model: 'mock-advanced', count: 4 }, { model: 'mock-fast', count: 1 }],
      retriesTotal: 5,
      fallbacks: [{ from: 'mock-advanced', to: 'mock-balanced', count: 3 }],
      fallbacksTotal: 3,
      errorKinds: [{ kind: 'unavailable', count: 6 }, { kind: 'timeout', count: 1 }],
    })
    expect(routerCounters(parsePrometheus('other_metric 1')).recognised).toBe(false)
  })
})
