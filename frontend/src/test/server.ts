import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'

import type { AnalyticsSummaryResponse, CacheStatsResponse, DependenciesResponse, HealthResponse, ModelOut, ModelsResponse } from '../api/types'
import { CAPTURED, clone, settingsFixture } from './budgetFixtures'

/* Fixtures for TESTS ONLY (test files are excluded from the build and the bundle check verifies it). */

export const HEALTH_FIXTURE: HealthResponse = {
  status: 'ok', app_name: 'Distributed LLM Router', version: '0.1.0', environment: 'test',
  timestamp: '2026-11-09T00:00:00Z', uptime_seconds: 1,
}

export const EMPTY_SUMMARY: AnalyticsSummaryResponse = {
  start: null, end: null, total_requests: 0, successful_requests: 0, failed_requests: 0, total_input_tokens: 0,
  total_output_tokens: 0, total_tokens: 0, estimated_total_cost: 0, average_latency_ms: null, cache_hits: 0,
  cache_misses: 0, cache_hit_rate: null, fallback_requests: 0, fallback_rate: null, requests_by_complexity: {},
  requests_by_model: [], provider_attempts: [],
}

export function modelFixture(id: string, overrides: Partial<ModelOut> = {}): ModelOut {
  return {
    id, provider: 'mock', provider_model: `mock/${id}`, tier: 'SIMPLE', enabled: true, capabilities: ['chat'], priority: 10,
    input_cost_per_million: 0.1, output_cost_per_million: 0.4, pricing_is_placeholder: true, max_context_tokens: 8192,
    max_output_tokens: 1024, timeout_seconds: 30, fallbacks: [], description: '',
    health: { state: 'healthy', consecutive_failures: 0, total_successes: 0, total_failures: 0, last_error_kind: null },
    ...overrides,
  }
}

export const MODELS_FIXTURE: ModelsResponse = {
  mock_mode: true, routing_strategy: 'priority', health_backend: 'memory',
  models: [modelFixture('mock-fast'), modelFixture('mock-advanced', { tier: 'COMPLEX' }), modelFixture('mock-legacy', { enabled: false })],
}

export const CACHE_FIXTURE: CacheStatsResponse = {
  enabled: true, semantic_enabled: false, available: true, embedding_provider: 'local-hashing-256', embedding_is_semantic: false,
  similarity_threshold: 0.92, ttl_seconds: 3600, total_entries: 0, scopes: 0, hits: 0, hits_exact: 0, hits_semantic: 0,
  misses: 0, stores: 0, hit_rate: null, approx_storage_bytes: 0, notes: [],
}

/** Shape of GET /api/v1/health/dependencies (backend/app/schemas/infra.py). */
export const DEPENDENCIES_FIXTURE: DependenciesResponse = {
  redis: { enabled: true, reachable: true }, database: { enabled: true, reachable: true }, cache_enabled: true,
  rate_limit_enabled: true, health_backend: 'redis', persistence: { written: 0, dropped: 0, failed: 0 },
}

/** Small, realistic /metrics body (Prometheus text format) with zero router counters. */
export const METRICS_FIXTURE = [
  '# HELP llm_router_build_info Build information (value is always 1).',
  '# TYPE llm_router_build_info gauge',
  'llm_router_build_info{mock_mode="true",version="0.1.0"} 1.0',
  '',
].join('\n')

export const defaultHandlers = [
  http.get('/health', () => HttpResponse.json(HEALTH_FIXTURE, { headers: { 'X-Request-ID': 'srv-health' } })),
  http.get('/api/v1/analytics/summary', () => HttpResponse.json(EMPTY_SUMMARY)),
  http.get('/api/v1/models', () => HttpResponse.json(MODELS_FIXTURE)),
  http.get('/api/v1/cache/stats', () => HttpResponse.json(CACHE_FIXTURE)),
  http.get('/api/v1/health/dependencies', () => HttpResponse.json(DEPENDENCIES_FIXTURE)),
  http.get('/metrics', () => new HttpResponse(METRICS_FIXTURE, { headers: { 'Content-Type': 'text/plain; version=0.0.4; charset=utf-8' } })),
  // Real captured body (src/test/backendCapture.json): the Settings page loads it on /settings.
  http.get('/api/v1/settings', () => HttpResponse.json(settingsFixture())),
  // Real captured bodies (empty history, mock mode): the Budget page loads all three on /budget.
  http.get('/api/v1/budget/status', () => HttpResponse.json(clone(CAPTURED.status))),
  http.get('/api/v1/budget/spend', () => HttpResponse.json(clone(CAPTURED.spend))),
  http.get('/api/v1/budget/refusals', () => HttpResponse.json(clone(CAPTURED.refusals))),
]

/** Unhandled requests FAIL the test: nothing can reach a real backend or provider. */
export const server = setupServer(...defaultHandlers)

/** Builds the backend's standard error envelope. */
export function errorResponse(status: number, code: string, message: string, init: { details?: unknown; headers?: Record<string, string> } = {}) {
  return HttpResponse.json({ error: { code, message, ...(init.details !== undefined ? { details: init.details } : {}) } },
    { status, headers: init.headers })
}
