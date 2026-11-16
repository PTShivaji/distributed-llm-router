/*
 * Fixtures for the Settings / Budget API tests (TESTS ONLY: src/test is excluded from the build).
 *
 * backendCapture.json holds REAL responses of the four endpoints, captured from the backend built in-process
 * with explicit throwaway settings (mock mode, no tokens, Redis off, the isolated empty test database, read
 * only). The richer fixtures below start from those bodies and change values only - never the shape.
 */

import type {
  BudgetRefusalsResponse, BudgetSpendResponse, BudgetStatusResponse, RefusalEvent, SettingsResponse, SpendDay,
} from '../api/types'
import capture from './backendCapture.json'

export const CAPTURED = capture as unknown as {
  settings: SettingsResponse
  status: BudgetStatusResponse
  spend: BudgetSpendResponse
  refusals: BudgetRefusalsResponse
}

export const clone = <T>(value: T): T => structuredClone(value)

export function settingsFixture(): SettingsResponse {
  return clone(CAPTURED.settings)
}

/** Budget configured (in-memory store), nothing counted yet: a real $0 with $5 remaining. */
export function statusFixture(): BudgetStatusResponse {
  return clone(CAPTURED.status)
}

/** Redis unreachable: values unknown (null), with the backend's reason. */
export function statusUnavailableFixture(): BudgetStatusResponse {
  const s = statusFixture()
  s.enforcement.store = 'redis'
  s.enforcement.store_scope = 'shared_across_instances'
  s.today = { available: false, source: 'budget_counters', counted_usd: null, remaining_usd: null,
    unavailable_reason: 'the budget store (Redis) is unreachable', per_client_counters_exposed: false }
  return s
}

/** No limits configured at all. */
export function statusNotConfiguredFixture(): BudgetStatusResponse {
  const s = statusFixture()
  s.enforcement = { ...s.enforcement, configured: false, daily_tracking: false, store: null, store_scope: null }
  s.limits = { daily_usd: null, daily_usd_per_client: null, daily_tokens_per_client: null, max_cost_per_request_usd: null }
  s.today = { available: false, source: 'budget_counters', counted_usd: null, remaining_usd: null,
    unavailable_reason: 'no spend limit is configured, so spend is not tracked', per_client_counters_exposed: false }
  return s
}

function day(date: string, overrides: Partial<SpendDay> = {}): SpendDay {
  return { date, requests: 0, successful_requests: 0, total_tokens: 0, mock_requests: 0, real_requests: 0,
    mock_estimated_cost_usd: 0, real_estimated_cost_usd: 0, placeholder_priced_requests: 0, cost_unknown_requests: 0,
    ...overrides }
}

/** Mixed history: mock rows (simulated) on day 1, real-call estimates for two providers on day 2. */
export function spendFixture(): BudgetSpendResponse {
  const s = clone(CAPTURED.spend)
  s.start_date = '2026-11-01'
  s.end_date = '2026-11-03'
  s.days = [
    day('2026-11-01', { requests: 2, successful_requests: 2, total_tokens: 100, mock_requests: 2,
      mock_estimated_cost_usd: 0.005, placeholder_priced_requests: 2 }),
    day('2026-11-02', { requests: 4, successful_requests: 3, total_tokens: 160, mock_requests: 1, real_requests: 3,
      real_estimated_cost_usd: 0.05, cost_unknown_requests: 1 }),
    day('2026-11-03'),
  ]
  s.totals = { requests: 6, successful_requests: 5, total_tokens: 260, mock_requests: 3, real_requests: 3,
    mock_estimated_cost_usd: 0.005, real_estimated_cost_usd: 0.05, placeholder_priced_requests: 2, cost_unknown_requests: 1 }
  s.by_provider = [
    { name: 'openai', requests: 2, total_tokens: 110, mock_estimated_cost_usd: 0, real_estimated_cost_usd: 0.02, placeholder_priced_requests: 0 },
    { name: 'mock', requests: 2, total_tokens: 100, mock_estimated_cost_usd: 0.005, real_estimated_cost_usd: 0, placeholder_priced_requests: 2 },
    { name: 'anthropic', requests: 1, total_tokens: 50, mock_estimated_cost_usd: 0, real_estimated_cost_usd: 0.03, placeholder_priced_requests: 0 },
  ]
  s.by_model = s.by_provider.map((g) => ({ ...g, name: `${g.name}-model` }))
  s.cost_basis = 'mixed: mock rows are simulated at placeholder prices; real rows are estimates, not invoices'
  return s
}

export function event(overrides: Partial<RefusalEvent> = {}): RefusalEvent {
  return { time: '2026-11-03T10:00:00Z', reason: 'global_daily_budget', category: 'global_daily',
    description: 'The global daily USD limit would have been exceeded.', model: 'gpt-x', provider: 'openai',
    attempt_number: 1, request_outcome: 'refused', mock_mode: false, ...overrides }
}

export function refusalsFixture(): BudgetRefusalsResponse {
  const r = clone(CAPTURED.refusals)
  r.events = [
    event(),
    event({ time: '2026-11-03T09:00:00Z', reason: 'per_request_limit', category: 'per_request',
      description: 'Worst-case cost of the request exceeded BUDGET_MAX_COST_PER_REQUEST_USD.',
      request_outcome: 'served_by_another_model' }),
  ]
  r.total_refused_attempts = 3
  r.refused_requests = 2
  r.by_reason = [{ key: 'global_daily_budget', count: 2 }, { key: 'per_request_limit', count: 1 }]
  r.by_provider = [{ key: 'openai', count: 3 }]
  r.pagination = { page: 1, page_size: 2, total: 3, has_more: true }
  return r
}
