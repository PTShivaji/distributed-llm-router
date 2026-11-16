import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AnalyticsSummaryResponse } from '../api/types'
import { credentialStore } from '../auth/credentials'
import { toLocalInput } from '../lib/analytics'
import { renderApp } from '../test/render'
import { EMPTY_SUMMARY, errorResponse, MODELS_FIXTURE, modelFixture, server } from '../test/server'

/* Mocked API data only. */
const SUMMARY: AnalyticsSummaryResponse = {
  ...EMPTY_SUMMARY,
  total_requests: 20, successful_requests: 18, failed_requests: 2, total_input_tokens: 3000, total_output_tokens: 1000, total_tokens: 4000,
  estimated_total_cost: 0.0123, average_latency_ms: 210.4, cache_hits: 3, cache_misses: 9, cache_hit_rate: 0.25, fallback_requests: 2, fallback_rate: 0.1,
  requests_by_complexity: { COMPLEX: 5, unknown: 1, SIMPLE: 10, MEDIUM: 4 },
  requests_by_model: [
    { model: 'mock-fast', requests: 12, total_tokens: 1200, estimated_cost: 0.0012, average_latency_ms: 50.5 },
    { model: 'mock-advanced', requests: 6, total_tokens: 2800, estimated_cost: 0.0111, average_latency_ms: 600 },
  ],
  provider_attempts: [
    { provider: 'mock', successes: 18, failures: 2, skipped: 3 },
    { provider: 'openai', successes: 0, failures: 0, skipped: 4 },
  ],
}

const METRICS = [
  'llm_router_build_info{mock_mode="true",version="0.1.0"} 1.0',
  'llm_router_retries_total{model="mock-advanced"} 4.0',
  'llm_router_fallbacks_total{from_model="mock-advanced",to_model="mock-balanced"} 3.0',
  'llm_router_provider_attempts_total{error_kind="unavailable",model="mock-advanced",outcome="error",provider="mock"} 6.0',
  'llm_router_chat_requests_total{cache_status="miss",complexity="SIMPLE",model_used="mock-fast",outcome="success",selected_model="mock-fast"} 11.0',
  '',
].join('\n')

interface Call { start: string | null; end: string | null; auth: string | null }

function serveAnalytics(respond: (call: Call, n: number) => Response = () => HttpResponse.json(SUMMARY)) {
  const calls: Call[] = []
  server.use(http.get('/api/v1/analytics/summary', ({ request }) => {
    const url = new URL(request.url)
    const call = { start: url.searchParams.get('start'), end: url.searchParams.get('end'), auth: request.headers.get('authorization') }
    calls.push(call)
    return respond(call, calls.length)
  }))
  return calls
}

function countRequests() {
  const counts = { models: 0, metrics: 0 }
  server.use(
    http.get('/api/v1/models', () => {
      counts.models += 1
      return HttpResponse.json(MODELS_FIXTURE)
    }),
    http.get('/metrics', () => {
      counts.metrics += 1
      return new HttpResponse(METRICS, { headers: { 'Content-Type': 'text/plain' } })
    }),
  )
  return counts
}

const kpi = (label: string) => screen.findByTestId(`kpi-${label}`)
const span = (c: Call) => Date.parse(c.end!) - Date.parse(c.start!)
const HOUR = 3_600_000

async function openPage(path = '/analytics') {
  const user = userEvent.setup()
  const utils = renderApp(path)
  await screen.findByRole('heading', { level: 1, name: 'Routing Analytics' })
  return { user, ...utils }
}

afterEach(() => vi.useRealTimers())

describe('Routing Analytics: data and calculations', () => {
  it('loads the last 7 days by default and shows the KPIs from the summary', async () => {
    const calls = serveAnalytics()
    const counts = countRequests()
    await openPage()
    expect(await kpi('Total requests')).toHaveTextContent('20')
    expect(await kpi('Successful')).toHaveTextContent('18')
    expect(screen.getByText('90.0% of requests')).toBeInTheDocument()
    expect(await kpi('Failed')).toHaveTextContent('2')
    expect(await kpi('Average latency')).toHaveTextContent('210 ms')
    expect(await kpi('Estimated cost')).toHaveTextContent('$0.0123')
    expect(await kpi('Cache hit rate')).toHaveTextContent('25.0%')
    expect(screen.getByText('3 hits · 9 misses')).toBeInTheDocument()
    expect(await kpi('Fallback rate')).toHaveTextContent('10.0%')
    expect(await kpi('Total tokens')).toHaveTextContent('4,000')
    expect(calls).toHaveLength(1)
    expect(span(calls[0])).toBe(7 * 24 * HOUR)
    expect(Math.abs(Date.parse(calls[0].end!) - Date.now())).toBeLessThan(10_000)
    expect(within(screen.getByRole('radiogroup', { name: 'Date range' })).getByRole('radio', { name: 'Last 7 days' })).toHaveAttribute('aria-checked', 'true')
    await waitFor(() => expect(counts).toEqual({ models: 1, metrics: 1 }))
  })

  it('shows requests by complexity in tier order with shares (chart + table)', async () => {
    serveAnalytics()
    await openPage()
    const table = await screen.findByRole('table', { name: 'Requests by complexity (table)' })
    expect(within(table).getAllByRole('row').slice(1).map((r) => r.textContent)).toEqual([
      'SIMPLE1050.0%', 'MEDIUM420.0%', 'COMPLEX525.0%', 'unknown15.0%',
    ])
    expect(screen.getByRole('figure', { name: 'Requests by complexity' })).toBeInTheDocument()
  })

  it('shows requests by model with providers joined from the registry', async () => {
    serveAnalytics()
    countRequests()
    await openPage()
    const figure = await screen.findByRole('figure', { name: 'Requests by model' })
    await waitFor(() => expect(within(figure).getAllByText('mock').length).toBe(2))
    expect(figure).toHaveTextContent('mock-fast')
    expect(figure).toHaveTextContent('$0.0111')
    expect(figure).toHaveTextContent('600 ms')
  })

  it('shows provider attempts with call success (skipped excluded) and totals', async () => {
    serveAnalytics()
    await openPage()
    const table = await screen.findByRole('table', { name: 'Provider attempts (table)' })
    const rows = within(table).getAllByRole('row').map((r) => r.textContent)
    expect(rows).toContain('mock18232390.0%')
    expect(rows).toContain('openai0044—')
    expect(rows).toContain('All providers18272790.0%')
    expect(screen.getByTestId('stat-Requests with fallback')).toHaveTextContent('2')
    expect(screen.getByTestId('stat-Skipped candidates')).toHaveTextContent('7')
  })

  it('shows token usage, per-request averages, placeholder pricing and the budget limitation', async () => {
    serveAnalytics()
    countRequests()
    await openPage()
    const card = await screen.findByTestId('usage-card')
    expect(card).toHaveTextContent('Input tokens3,000')
    expect(card).toHaveTextContent('Output tokens1,000')
    expect(card).toHaveTextContent('Average tokens per request200')
    expect(card).toHaveTextContent('Average estimated cost per request$0.000615')
    expect(within(card).getByRole('img')).toHaveAccessibleName('Input 75.0% and output 25.0% of tokens')
    expect(await within(card).findByText(/placeholder pricing \(mock mode\)/)).toBeInTheDocument()
    expect(card).toHaveTextContent(/remaining budget .* not exposed by this API/)
  })

  it('shows missing values as unavailable, never as 0 or NaN', async () => {
    serveAnalytics(() => HttpResponse.json({ ...SUMMARY, average_latency_ms: null, cache_hits: 0, cache_misses: 0, cache_hit_rate: null,
      fallback_rate: null }))
    await openPage()
    expect(await kpi('Average latency')).toHaveTextContent('—')
    expect(await kpi('Cache hit rate')).toHaveTextContent('—')
    expect(screen.getByText('No cache lookups in range')).toBeInTheDocument()
    expect(await kpi('Fallback rate')).toHaveTextContent('—')
    expect(document.body.textContent).not.toMatch(/NaN|Infinity/)
  })

  it('shows an empty state with real zero counts when the range has no requests', async () => {
    serveAnalytics(() => HttpResponse.json({ ...EMPTY_SUMMARY, start: '2026-11-08T00:00:00Z', end: '2026-11-09T00:00:00Z' }))
    await openPage()
    expect(await screen.findByText('No requests in this range')).toBeInTheDocument()
    expect(await kpi('Total requests')).toHaveTextContent('0')
    expect(await kpi('Average latency')).toHaveTextContent('—')
    expect(screen.getByText('No requests in range')).toBeInTheDocument()
    expect(screen.queryByRole('figure')).not.toBeInTheDocument()
  })
})

describe('Routing Analytics: date filters', () => {
  it('switches presets with one analytics request each (models/metrics are not refetched)', async () => {
    const calls = serveAnalytics()
    const counts = countRequests()
    const { user, router } = await openPage()
    await kpi('Total requests')
    await user.click(screen.getByRole('radio', { name: 'Last 24 hours' }))
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(span(calls[1])).toBe(24 * HOUR)
    expect(router.state.location.search).toBe('?range=24h')
    await user.click(screen.getByRole('radio', { name: 'Last hour' }))
    await waitFor(() => expect(calls).toHaveLength(3))
    expect(span(calls[2])).toBe(HOUR)
    await user.click(screen.getByRole('radio', { name: 'All time' }))
    await waitFor(() => expect(calls).toHaveLength(4))
    expect(calls[3]).toMatchObject({ start: null, end: null })
    expect(await screen.findByText(/All persisted request history/)).toBeInTheDocument()
    expect(counts).toEqual({ models: 1, metrics: 1 })
  })

  it('applies a valid custom range only on Apply, and rejects an inverted one without a request', async () => {
    const calls = serveAnalytics()
    const { user, router } = await openPage()
    await kpi('Total requests')
    await user.click(screen.getByRole('radio', { name: 'Custom' }))
    const form = screen.getByRole('form', { name: 'Custom date range' })
    const start = within(form).getByLabelText('Start (inclusive)')
    const end = within(form).getByLabelText('End (exclusive)')
    fireEvent.change(start, { target: { value: '2026-11-05T10:00' } })
    fireEvent.change(end, { target: { value: '2026-11-04T10:00' } })
    expect(calls).toHaveLength(1) // editing never sends
    await user.click(within(form).getByRole('button', { name: 'Apply range' }))
    expect(within(form).getByRole('alert')).toHaveTextContent('The start must be earlier than the end.')
    expect(calls).toHaveLength(1)
    fireEvent.change(end, { target: { value: '2026-11-06T10:00' } })
    await user.click(within(form).getByRole('button', { name: 'Apply range' }))
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[1]).toMatchObject({ start: new Date('2026-11-05T10:00').toISOString(), end: new Date('2026-11-06T10:00').toISOString() })
    expect(router.state.location.search).toContain('range=custom')
  })

  it('restores a custom range from the URL exactly', async () => {
    const calls = serveAnalytics(() => HttpResponse.json({ ...SUMMARY, start: '2026-11-01T00:00:00Z', end: '2026-11-02T00:00:00Z' }))
    await openPage('/analytics?range=custom&start=2026-11-01T00:00:00.000Z&end=2026-11-02T00:00:00.000Z')
    await kpi('Total requests')
    expect(calls[0]).toMatchObject({ start: '2026-11-01T00:00:00.000Z', end: '2026-11-02T00:00:00.000Z' })
    expect(screen.getByRole('radio', { name: 'Custom' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByLabelText('Start (inclusive)')).toHaveValue(toLocalInput(new Date('2026-11-01T00:00:00Z')))
    expect(screen.getByTestId('range-window')).toHaveTextContent('(end exclusive)')
  })

  it('never shows another range\'s data when a range change fails', async () => {
    serveAnalytics((_c, n) => (n === 1 ? HttpResponse.json(SUMMARY) : errorResponse(503, 'database_unavailable', 'Database is disabled or unreachable.')))
    const { user } = await openPage()
    expect(await kpi('Total requests')).toHaveTextContent('20')
    await user.click(screen.getByRole('radio', { name: 'Last 24 hours' }))
    expect(await screen.findByText('Service unavailable')).toBeInTheDocument()
    expect(screen.getByText('PostgreSQL is disabled or unreachable.')).toBeInTheDocument()
    expect(await kpi('Total requests')).toHaveTextContent('—')
    expect(screen.queryByRole('figure')).not.toBeInTheDocument()
    expect(screen.getByText('Not loaded')).toBeInTheDocument()
  })

  it('keeps the same range\'s data when only a refresh fails', async () => {
    serveAnalytics((_c, n) => (n === 1 ? HttpResponse.json(SUMMARY) : errorResponse(503, 'database_unavailable', 'down')))
    const { user } = await openPage()
    await kpi('Total requests')
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('Refresh failed · showing earlier data')).toBeInTheDocument()
    expect(await kpi('Total requests')).toHaveTextContent('20')
  })
})

describe('Routing Analytics: process counters from /metrics', () => {
  it('shows retries, fallbacks and error kinds, labelled as since process start and not range-filtered', async () => {
    serveAnalytics()
    countRequests()
    await openPage()
    const card = await screen.findByTestId('process-counters')
    await waitFor(() => expect(within(card).getByTestId('counter-Retries')).toHaveTextContent('4'))
    expect(within(card).getByTestId('counter-Fallbacks')).toHaveTextContent('3')
    expect(within(card).getByTestId('counter-Chat requests')).toHaveTextContent('11')
    expect(within(card).getByRole('list', { name: 'Fallbacks' })).toHaveTextContent('mock-advancedmock-balanced3')
    expect(within(card).getByRole('list', { name: 'Failed attempts by error kind' })).toHaveTextContent('unavailable6')
    expect(card).toHaveTextContent(/since it started; not filtered by the date range/)
    expect(await within(card).findByText(/Process up/)).toBeInTheDocument()
  })

  it('explains when metrics are disabled (404) without affecting analytics', async () => {
    serveAnalytics()
    server.use(http.get('/metrics', () => errorResponse(404, 'not_found', 'Not Found')))
    await openPage()
    expect(await screen.findByText('Metrics are disabled on this backend')).toBeInTheDocument()
    expect(await kpi('Total requests')).toHaveTextContent('20')
  })

  it('shows "no retries/fallbacks" for a fresh process instead of zeros per model', async () => {
    serveAnalytics()
    await openPage() // default /metrics fixture has no router counters except build_info
    const card = await screen.findByTestId('process-counters')
    expect(await within(card).findByText('No retries recorded.')).toBeInTheDocument()
    expect(within(card).getByText('No fallbacks recorded.')).toBeInTheDocument()
  })
})

describe('Routing Analytics: errors', () => {
  it('401 offers the observability token and retries with it after the user saves it', async () => {
    const calls = serveAnalytics((c) => (c.auth ? HttpResponse.json(SUMMARY) : errorResponse(401, 'unauthorized', 'Missing or invalid observability token')))
    server.use(http.get('/metrics', ({ request }) => (request.headers.get('authorization')
      ? new HttpResponse(METRICS) : errorResponse(401, 'unauthorized', 'token'))))
    const { user } = await openPage()
    const prompts = await screen.findAllByRole('group', { name: 'Observability token required' })
    expect(prompts.length).toBeGreaterThanOrEqual(1)
    await user.type(within(prompts[0]).getByPlaceholderText('Observability token'), 'obs-token-1234567890')
    await user.click(within(prompts[0]).getByRole('button', { name: 'Use token' }))
    expect(await kpi('Total requests')).toHaveTextContent('20')
    expect(calls.at(-1)!.auth).toBe('Bearer obs-token-1234567890')
    expect(credentialStore.has('observability')).toBe(true)
    await waitFor(() => expect(screen.getByTestId('counter-Retries')).toHaveTextContent('4'))
  })

  it.each([
    [429, 'rate_limit_exceeded', 'Rate limit reached', { 'Retry-After': '12' }, 'Try again in 12s.'],
    [400, 'invalid_time_range', 'Something went wrong', undefined, 'start must be earlier than end.'],
    [503, 'database_unavailable', 'Service unavailable', undefined, 'PostgreSQL is disabled or unreachable.'],
  ])('HTTP %s %s is shown without data', async (status, code, title, headers, detail) => {
    serveAnalytics(() => errorResponse(status, code, code === 'invalid_time_range' ? 'start must be earlier than end.' : 'msg', { headers }))
    await openPage()
    expect(await screen.findByText(title)).toBeInTheDocument()
    expect(screen.getByText(detail)).toBeInTheDocument()
    expect(await kpi('Total requests')).toHaveTextContent('—')
    expect(screen.getByTestId('range-window')).toHaveTextContent('Range not loaded')
  })

  it('network failure shows "Backend unreachable"', async () => {
    serveAnalytics(() => HttpResponse.error())
    await openPage()
    expect((await screen.findAllByText('Backend unreachable')).length).toBeGreaterThan(0)
  })
})

describe('Routing Analytics: requests and layout', () => {
  it('does not poll: no further requests while the page stays open', async () => {
    const calls = serveAnalytics()
    const counts = countRequests()
    await openPage()
    await kpi('Total requests')
    await waitFor(() => expect(counts.metrics).toBe(1))
    vi.useFakeTimers()
    vi.advanceTimersByTime(15 * 60_000)
    vi.useRealTimers()
    await new Promise((r) => setTimeout(r, 50))
    expect(calls).toHaveLength(1)
    expect(counts).toEqual({ models: 1, metrics: 1 })
  })

  it('Refresh reloads analytics, models and metrics once each', async () => {
    const calls = serveAnalytics()
    const counts = countRequests()
    const { user } = await openPage()
    await kpi('Total requests')
    await waitFor(() => expect(counts.metrics).toBe(1))
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(calls).toHaveLength(2))
    await waitFor(() => expect(counts).toEqual({ models: 2, metrics: 2 }))
  })

  it('uses responsive grids (2→4 KPI columns, stacked → 2 chart columns on xl)', async () => {
    serveAnalytics()
    await openPage()
    const kpis = (await kpi('Total requests')).closest('.grid')!
    expect(kpis.className).toContain('grid-cols-2')
    expect(kpis.className).toContain('md:grid-cols-4')
    const chartGrid = (await screen.findByRole('figure', { name: 'Requests by complexity' })).closest('.xl\\:grid-cols-2')!
    expect(chartGrid.className).toContain('grid-cols-1')
  })

  it('lists models used only in analytics even if missing from the registry', async () => {
    serveAnalytics(() => HttpResponse.json({ ...SUMMARY, requests_by_model: [{ model: 'retired-model', requests: 1, total_tokens: 0, estimated_cost: 0, average_latency_ms: null }] }))
    server.use(http.get('/api/v1/models', () => HttpResponse.json({ ...MODELS_FIXTURE, models: [modelFixture('mock-fast')] })))
    await openPage()
    const figure = await screen.findByRole('figure', { name: 'Requests by model' })
    await waitFor(() => expect(within(figure).getByText('not in registry')).toBeInTheDocument())
  })
})
