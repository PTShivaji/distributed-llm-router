import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { sessionActivity } from '../activity/sessionActivity'
import type { AnalyticsSummaryResponse } from '../api/types'
import { renderApp } from '../test/render'
import { AUTO_REFRESH_MS } from './OverviewPage'
import { CACHE_FIXTURE, EMPTY_SUMMARY, errorResponse, MODELS_FIXTURE, modelFixture, server } from '../test/server'

const SUMMARY: AnalyticsSummaryResponse = {
  ...EMPTY_SUMMARY,
  total_requests: 1250, successful_requests: 1200, failed_requests: 50, total_tokens: 98_000, estimated_total_cost: 0.0421,
  average_latency_ms: 182.4, cache_hits: 300, cache_misses: 900, cache_hit_rate: 0.25, fallback_requests: 40, fallback_rate: 0.032,
  requests_by_model: [
    { model: 'mock-fast', requests: 400, total_tokens: 20_000, estimated_cost: 0.002, average_latency_ms: 12.5 },
    { model: 'mock-advanced', requests: 800, total_tokens: 78_000, estimated_cost: 0.0401, average_latency_ms: 240 },
    { model: 'retired-model', requests: 50, total_tokens: 0, estimated_cost: 0, average_latency_ms: null },
  ],
}

/** Records every analytics call; windowed calls (start/end) get `perWindow(start)`. */
function analytics(all: AnalyticsSummaryResponse, perWindow: (start: Date) => Partial<AnalyticsSummaryResponse> = () => ({})) {
  const calls: { start: string | null; end: string | null; auth: string | null }[] = []
  server.use(http.get('/api/v1/analytics/summary', ({ request }) => {
    const url = new URL(request.url)
    const start = url.searchParams.get('start')
    calls.push({ start, end: url.searchParams.get('end'), auth: request.headers.get('authorization') })
    return HttpResponse.json(start ? { ...EMPTY_SUMMARY, ...perWindow(new Date(start)) } : all)
  }))
  return calls
}

const kpi = (label: string) => screen.findByTestId(`kpi-${label}`)

describe('Overview: data from the backend', () => {
  it('renders the header, refresh control and real KPI values', async () => {
    analytics(SUMMARY)
    renderApp('/')
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
    expect(await kpi('Total requests')).toHaveTextContent('1,250')
    expect(await kpi('Successful')).toHaveTextContent('1,200')
    expect(await kpi('Failed')).toHaveTextContent('50')
    expect(await kpi('Average latency')).toHaveTextContent('182 ms')
    expect(await kpi('Estimated cost')).toHaveTextContent('$0.0421')
    expect(await kpi('Cache hit rate')).toHaveTextContent('25.0%')
    expect(await kpi('Fallback rate')).toHaveTextContent('3.2%')
    expect(await kpi('Active models')).toHaveTextContent('2 / 3')
    expect(screen.getByText('96.0% of total')).toBeInTheDocument()
    expect(screen.getByText('Estimate · placeholder pricing')).toBeInTheDocument()
    expect(await screen.findByText(/^Updated /)).toBeInTheDocument()
  })

  it('shows skeletons (no numbers) while loading', async () => {
    server.use(http.get('/api/v1/analytics/summary', async () => {
      await delay('infinite')
      return HttpResponse.json(EMPTY_SUMMARY)
    }))
    renderApp('/')
    expect(await screen.findByLabelText('Total requests loading')).toBeInTheDocument()
    expect(screen.queryByTestId('kpi-Total requests')).not.toBeInTheDocument()
    expect(await kpi('Active models')).toHaveTextContent('2 / 3') // independent sections load independently
  })

  it('builds the activity chart from 24 aligned hourly windows (real aggregates only)', async () => {
    const calls = analytics(SUMMARY, (start) => (start.getUTCHours() % 6 === 0 ? { total_requests: 9, successful_requests: 7, failed_requests: 2 } : {}))
    renderApp('/')
    const figure = await screen.findByRole('figure', { name: 'Requests per hour (last 24 hours)' })
    const windowed = calls.filter((c) => c.start)
    expect(windowed).toHaveLength(24)
    for (const c of windowed) {
      const start = new Date(c.start!)
      expect(start.getUTCMinutes() + start.getUTCSeconds() + start.getUTCMilliseconds()).toBe(0)
      expect(new Date(c.end!).getTime() - start.getTime()).toBe(3_600_000)
    }
    const rows = within(figure).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(24)
    const totals = rows.map((r) => Number(within(r).getAllByRole('cell')[1].textContent))
    expect(totals.filter((t) => t === 9)).toHaveLength(4)
    expect(totals.filter((t) => t !== 9 && t !== 0)).toHaveLength(0)
    expect(screen.getByText('Aggregated API data')).toBeInTheDocument()
  })

  it('shows model usage sorted by requests, joined with registry providers', async () => {
    analytics(SUMMARY)
    renderApp('/')
    const figure = await screen.findByRole('figure', { name: 'Requests by model' })
    const rows = within(figure).getAllByRole('row').slice(1).map((r) => within(r).getAllByRole('cell').map((c) => c.textContent))
    expect(rows.map((r) => r[0])).toEqual(['mock-advanced', 'mock-fast', 'retired-model'])
    expect(rows[0]).toEqual(['mock-advanced', 'mock', '800', '78,000', '$0.0401', '240 ms'])
    expect(rows[2][1]).toBe('not in registry')
    expect(rows[2][5]).toBe('—')
  })

  it('separates registry status from router health', async () => {
    server.use(http.get('/api/v1/models', () => HttpResponse.json({ ...MODELS_FIXTURE, health_backend: 'redis', models: [
      modelFixture('mock-fast', { health: { state: 'unhealthy', consecutive_failures: 3, total_successes: 5, total_failures: 4, last_error_kind: 'timeout' } }),
      modelFixture('openai-small', { provider: 'openai', enabled: false }),
    ] })))
    renderApp('/')
    const fast = await screen.findByTestId('health-mock-fast')
    expect(within(fast).getByText('Enabled')).toBeInTheDocument()
    expect(within(fast).getByText('unhealthy')).toBeInTheDocument()
    expect(within(fast).getByText('last error: timeout')).toBeInTheDocument()
    expect(within(fast).getByText('3 / 4')).toBeInTheDocument()
    const disabled = screen.getByTestId('health-openai-small')
    expect(within(disabled).getByText('Disabled')).toBeInTheDocument()
    expect(within(disabled).getByText('not routed')).toBeInTheDocument()
    expect(screen.getByText(/shared via Redis/)).toBeInTheDocument()
    expect(screen.getByText('openai: 0/1 enabled')).toBeInTheDocument()
  })

  it('shows cache statistics and whether semantic matching is on', async () => {
    server.use(http.get('/api/v1/cache/stats', () => HttpResponse.json({ ...CACHE_FIXTURE, hits_exact: 30, hits_semantic: 5,
      misses: 65, hit_rate: 0.35, semantic_enabled: true, total_entries: 12, notes: ['Approximate matching uses a meaning-aware embedder.'] })))
    renderApp('/')
    expect(await screen.findByTestId('cache-Exact hits')).toHaveTextContent('30')
    expect(screen.getByTestId('cache-Semantic hits')).toHaveTextContent('5')
    expect(screen.getByTestId('cache-Misses')).toHaveTextContent('65')
    expect(screen.getByTestId('cache-Hit rate')).toHaveTextContent('35.0%')
    expect(screen.getByText('Semantic matching on')).toBeInTheDocument()
    expect(screen.getByText(/meaning-aware embedder/)).toBeInTheDocument()
  })
})

describe('Overview: empty data is never filled in', () => {
  it('shows zeros, dashes and empty states for a fresh backend', async () => {
    renderApp('/')
    expect(await kpi('Total requests')).toHaveTextContent(/^0$/)
    expect(await kpi('Average latency')).toHaveTextContent('—')
    expect(await kpi('Cache hit rate')).toHaveTextContent('—')
    expect(screen.getByText('No requests yet')).toBeInTheDocument()
    expect(await screen.findByText('No requests in the last 24 hours')).toBeInTheDocument()
    expect(screen.getByText('No model usage recorded yet')).toBeInTheDocument()
    expect(screen.getByText('No activity in this session')).toBeInTheDocument()
    expect(screen.queryByRole('figure')).not.toBeInTheDocument()
    expect(screen.queryByText('Demo data')).not.toBeInTheDocument()
  })

  it('a proxy 502 (backend down) is reported as "Backend unreachable", with dashes and no hints', async () => {
    const down = () => new HttpResponse(null, { status: 502 })
    server.use(http.get('/api/v1/analytics/summary', down), http.get('/api/v1/models', down), http.get('/api/v1/cache/stats', down),
      http.get('/health', down))
    renderApp('/')
    expect((await screen.findAllByText('Backend unreachable')).length).toBeGreaterThan(1)
    expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument()
    expect(await kpi('Average latency')).toHaveTextContent('—')
    expect(screen.queryByText('End-to-end, per request')).not.toBeInTheDocument()
  })

  it('shows dashes, not numbers, when analytics fail', async () => {
    server.use(http.get('/api/v1/analytics/summary', () => errorResponse(503, 'database_unavailable', 'Database is disabled or unreachable.')))
    renderApp('/')
    expect((await screen.findAllByText('Database is disabled or unreachable.')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('PostgreSQL is disabled or unreachable.').length).toBeGreaterThan(0)
    for (const label of ['Total requests', 'Successful', 'Failed', 'Estimated cost']) expect(await kpi(label)).toHaveTextContent('—')
    expect(await kpi('Active models')).toHaveTextContent('2 / 3') // models endpoint still works
  })
})

describe('Overview: authentication and rate limits', () => {
  it('401 offers inline observability-token entry, then retries with the token (memory only)', async () => {
    const TOKEN = 'observability-token-for-overview-01'
    server.use(http.get('/api/v1/analytics/summary', ({ request }) =>
      request.headers.get('authorization') === `Bearer ${TOKEN}` ? HttpResponse.json(SUMMARY)
        : errorResponse(401, 'unauthorized', 'Missing or invalid observability token.')))
    server.use(http.get('/api/v1/cache/stats', ({ request }) =>
      request.headers.get('authorization') === `Bearer ${TOKEN}` ? HttpResponse.json(CACHE_FIXTURE)
        : errorResponse(401, 'unauthorized', 'Missing or invalid observability token.')))
    const user = userEvent.setup()
    renderApp('/')
    const prompts = await screen.findAllByRole('group', { name: 'Observability token required' })
    await user.type(within(prompts[0]).getByPlaceholderText('Observability token'), TOKEN)
    await user.click(within(prompts[0]).getByRole('button', { name: 'Use token' }))
    expect(await kpi('Total requests')).toHaveTextContent('1,250')
    await waitFor(() => expect(screen.queryByRole('group', { name: 'Observability token required' })).not.toBeInTheDocument())
    expect(sessionStorage.length).toBe(0) // not persisted without explicit opt-in
  })

  it('429 shows the Retry-After wait', async () => {
    server.use(http.get('/api/v1/cache/stats', () => errorResponse(429, 'rate_limit_exceeded', 'Rate limit exceeded.', { headers: { 'Retry-After': '17' } })))
    renderApp('/')
    expect(await screen.findByText('Try again in 17s.')).toBeInTheDocument()
  })

  it('403 is shown as an error, not as data', async () => {
    server.use(http.get('/api/v1/cache/stats', () => errorResponse(403, 'forbidden', 'Forbidden.')))
    renderApp('/')
    expect(await screen.findByText('Not allowed')).toBeInTheDocument()
    expect(screen.queryByTestId('cache-Exact hits')).not.toBeInTheDocument()
  })
})

describe('Overview: requests and recent activity', () => {
  it('makes a bounded set of requests on open, and refetches everything on Refresh', async () => {
    const calls = analytics(EMPTY_SUMMARY)
    let models = 0
    let health = 0
    server.use(http.get('/api/v1/models', () => {
      models += 1
      return HttpResponse.json(MODELS_FIXTURE)
    }))
    server.use(http.get('/health', () => {
      health += 1
      return HttpResponse.json({ status: 'ok', app_name: 'x', version: '0.1.0', environment: 'test', timestamp: '2026-11-09T00:00:00Z', uptime_seconds: 1 })
    }))
    const user = userEvent.setup()
    renderApp('/')
    await screen.findByText('No requests in the last 24 hours')
    await waitFor(() => expect(calls).toHaveLength(25))
    expect({ allTime: calls.filter((c) => !c.start).length, models, health }).toEqual({ allTime: 1, models: 1, health: 1 })
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(calls).toHaveLength(50))
    await waitFor(() => expect({ models, health }).toEqual({ models: 2, health: 2 }))
  })

  it('lists only this session\'s activity, without prompt text', async () => {
    renderApp('/')
    expect(await screen.findByText('No activity in this session')).toBeInTheDocument()
    act(() => sessionActivity.record({ requestId: 'req-1', at: '2026-11-09T10:00:00Z', outcome: 'success', selectedModel: 'mock-fast',
      modelUsed: 'mock-fast', provider: 'mock', complexity: 'SIMPLE', cacheStatus: 'miss', latencyMs: 12.3, totalCost: 0.00002,
      fallbackUsed: false, errorCode: null }))
    const card = screen.getByRole('region', { name: 'Recent activity' })
    expect(within(card).getByText('OK')).toBeInTheDocument()
    expect(within(card).getByText('mock-fast')).toBeInTheDocument()
    expect(within(card).getByText('12 ms')).toBeInTheDocument()
    expect(screen.queryByText('No activity in this session')).not.toBeInTheDocument()
  })
})

describe('Overview: automatic refresh never reads /cache/stats (it prunes the cache)', () => {
  afterEach(() => vi.restoreAllMocks())

  /** Captures the Overview auto-refresh interval so a test can fire it without waiting 60 s. */
  function captureAutoRefresh() {
    const timers: { id: number; fn: () => void }[] = []
    const cleared = new Set<number>()
    const realSet = window.setInterval.bind(window)
    const realClear = window.clearInterval.bind(window)
    vi.spyOn(window, 'setInterval').mockImplementation(((fn: () => void, ms?: number) => {
      const id = realSet(() => {}, ms) as unknown as number // never fires on its own: the test fires it
      if (ms === AUTO_REFRESH_MS) timers.push({ id, fn })
      return id
    }) as typeof setInterval)
    vi.spyOn(window, 'clearInterval').mockImplementation((id?: number) => {
      if (id !== undefined) cleared.add(id)
      realClear(id)
    })
    return { timers, cleared, tick: () => act(() => { for (const t of timers.filter((x) => !cleared.has(x.id))) t.fn() }) }
  }

  function countRequests() {
    const n = { cache: 0, models: 0 }
    server.use(
      http.get('/api/v1/cache/stats', () => { n.cache += 1; return HttpResponse.json({ ...CACHE_FIXTURE, hits: n.cache }) }),
      http.get('/api/v1/models', () => { n.models += 1; return HttpResponse.json(MODELS_FIXTURE) }),
    )
    return n
  }

  it('initial load reads cache stats once; auto-refresh ticks reload summary and models but never cache stats; Refresh reloads them', async () => {
    const auto = captureAutoRefresh()
    const calls = analytics(EMPTY_SUMMARY)
    const n = countRequests()
    const user = userEvent.setup()
    renderApp('/')
    await screen.findByText('No requests in the last 24 hours')
    await waitFor(() => expect(calls).toHaveLength(25))
    await waitFor(() => expect(n).toEqual({ cache: 1, models: 1 }))
    expect(auto.timers).toHaveLength(1) // the existing 60 s auto-refresh is still on by default

    await auto.tick()
    await waitFor(() => expect(calls.filter((c) => !c.start)).toHaveLength(2))
    await waitFor(() => expect(n.models).toBe(2))
    await auto.tick()
    await waitFor(() => expect(calls.filter((c) => !c.start)).toHaveLength(3))
    await waitFor(() => expect(n.models).toBe(3))
    await act(() => new Promise((r) => setTimeout(r, 150)))
    expect(n.cache).toBe(1) // two automatic refreshes, zero cache-stat reads
    expect(calls.filter((c) => c.start)).toHaveLength(24) // the hourly chart is not auto-polled either (unchanged)

    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(n.cache).toBe(2)) // manual Refresh still reads cache stats
    await waitFor(() => expect(calls.filter((c) => !c.start)).toHaveLength(4))
    await waitFor(() => expect(n.models).toBe(4))

    await auto.tick()
    await waitFor(() => expect(n.models).toBe(5))
    await act(() => new Promise((r) => setTimeout(r, 150)))
    expect(n.cache).toBe(2)
  })

  it('keeps showing the last cache statistics (no loading or error state) across automatic refreshes', async () => {
    const auto = captureAutoRefresh()
    analytics(EMPTY_SUMMARY)
    const n = countRequests()
    renderApp('/')
    await screen.findByText('No requests in the last 24 hours')
    await waitFor(() => expect(n.cache).toBe(1))
    const cacheCard = () => screen.getByRole('heading', { name: 'Response cache' }).closest('.rounded-lg')!
    await screen.findByRole('heading', { name: 'Response cache' })
    const before = cacheCard().textContent
    await auto.tick()
    await waitFor(() => expect(n.models).toBe(2))
    await act(() => new Promise((r) => setTimeout(r, 150)))
    expect(cacheCard().textContent).toBe(before)
    expect(within(cacheCard() as HTMLElement).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('leaving the Overview clears its auto-refresh timer', async () => {
    const auto = captureAutoRefresh()
    analytics(EMPTY_SUMMARY)
    const n = countRequests()
    const user = userEvent.setup()
    renderApp('/')
    await screen.findByText('No requests in the last 24 hours')
    expect(auto.timers).toHaveLength(1)
    await user.click(within(screen.getByRole('navigation')).getByRole('link', { name: 'Model Registry' }))
    await screen.findByRole('heading', { level: 1, name: 'Model Registry' })
    expect(auto.cleared.has(auto.timers[0].id)).toBe(true)
    // The Registry page makes its own /models request: let it settle before taking the baseline.
    await waitFor(() => expect(n.models).toBe(2))
    await screen.findAllByText('mock-advanced')
    const models = n.models
    await auto.tick() // a stale callback would no longer be invoked; nothing is requested
    await act(() => new Promise((r) => setTimeout(r, 150)))
    expect(n).toEqual({ cache: 1, models })
  })
})
