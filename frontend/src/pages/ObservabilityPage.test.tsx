import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/errors'
import type { DependenciesResponse } from '../api/types'
import { credentialStore } from '../auth/credentials'
import { SectionFailure } from '../components/observability/StaleNotice'
import { REAL_METRICS } from '../test/metricsFixture'
import { renderApp } from '../test/render'
import { DEPENDENCIES_FIXTURE, errorResponse, server } from '../test/server'

/* Mocked API data only: REAL_METRICS is a verbatim capture from an isolated mock-mode backend. */
const TOKEN = 'obs-token-page-tests-0123456789ABCDEF'
const TEXT = { 'Content-Type': 'text/plain; version=0.0.4; charset=utf-8' }

const DB_DOWN: DependenciesResponse = { ...DEPENDENCIES_FIXTURE, database: { enabled: true, reachable: false }, persistence: { written: 0, dropped: 4, failed: 0 } }
const DB_DISABLED: DependenciesResponse = { ...DEPENDENCIES_FIXTURE, database: { enabled: false, reachable: false }, health_backend: 'memory' }

/** Only build info, an unlabelled zero counter, an empty family and a NaN ratio: what a fresh process exposes. */
const SPARSE_METRICS = [
  '# HELP llm_router_build_info Build information (value is always 1).',
  '# TYPE llm_router_build_info gauge',
  'llm_router_build_info{mock_mode="true",version="0.1.0"} 1.0',
  '# TYPE llm_router_http_requests_total counter',
  '# TYPE llm_router_http_requests_in_progress gauge',
  'llm_router_http_requests_in_progress 1.0',
  '# TYPE llm_router_cache_hit_ratio gauge',
  'llm_router_cache_hit_ratio NaN',
  '# TYPE llm_router_embedding_estimated_cost_usd_total counter',
  'llm_router_embedding_estimated_cost_usd_total 0.0',
  '',
].join('\n')

type Respond = (n: number, auth: string | null) => Response

function serve({ deps = () => HttpResponse.json(DEPENDENCIES_FIXTURE), metrics = () => new HttpResponse(REAL_METRICS, { headers: TEXT }) }:
  { deps?: Respond; metrics?: Respond } = {}) {
  const calls = { deps: [] as (string | null)[], metrics: [] as (string | null)[], urls: [] as string[] }
  server.use(
    http.get('/api/v1/health/dependencies', ({ request }) => {
      calls.deps.push(request.headers.get('authorization'))
      calls.urls.push(request.url)
      return deps(calls.deps.length, request.headers.get('authorization'))
    }),
    http.get('/metrics', ({ request }) => {
      calls.metrics.push(request.headers.get('authorization'))
      calls.urls.push(request.url)
      return metrics(calls.metrics.length, request.headers.get('authorization'))
    }),
  )
  return calls
}

const kpi = (label: string) => screen.findByTestId(`kpi-${label}`)
const state = (testId: string) => {
  const el = screen.getByTestId(testId)
  return el.getAttribute('data-state') ?? el.querySelector('[data-state]')?.getAttribute('data-state')
}

async function openPage() {
  const user = userEvent.setup()
  const utils = renderApp('/observability')
  await screen.findByRole('heading', { level: 1, name: 'Observability' })
  return { user, ...utils }
}

function expectTokenNowhere() {
  expect(document.documentElement.outerHTML).not.toContain(TOKEN)
  expect(window.localStorage.length).toBe(0)
  for (let i = 0; i < window.sessionStorage.length; i++) expect(window.sessionStorage.getItem(window.sessionStorage.key(i)!)).not.toContain(TOKEN)
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('Observability: health', () => {
  it('healthy: liveness, Redis, PostgreSQL and the other reported components', async () => {
    serve()
    await openPage()
    await waitFor(() => expect(state('overall-state')).toBe('healthy'))
    expect(state('dep-api')).toBe('healthy')
    expect(screen.getByTestId('dep-api')).toHaveTextContent(/v0\.1\.0 · test · up 0m 1s/)
    expect(state('dep-redis')).toBe('healthy')
    expect(state('dep-database')).toBe('healthy')
    expect(screen.getByTestId('dep-cache')).toHaveTextContent('Configured')
    expect(screen.getByTestId('dep-ratelimit')).toHaveTextContent('Configured')
    expect(screen.getByTestId('dep-health-backend')).toHaveTextContent(/redis.*Shared by all instances/)
    expect(screen.getByTestId('dep-persistence')).toHaveTextContent(/Written\s*0.*Dropped\s*0.*Failed\s*0/)
    expect(screen.getByTestId('health-card')).toHaveTextContent(/Dependencies last checked/)
  })

  it('degraded: an enabled dependency that is unreachable', async () => {
    serve({ deps: () => HttpResponse.json(DB_DOWN) })
    await openPage()
    await waitFor(() => expect(state('overall-state')).toBe('degraded'))
    expect(state('dep-database')).toBe('unavailable')
    expect(screen.getByTestId('overall-reasons')).toHaveTextContent('PostgreSQL is enabled but not reachable.')
    expect(screen.getByTestId('dep-persistence')).toHaveTextContent(/Dropped\s*4/)
  })

  it('a disabled dependency is "Disabled", not a failure, and its default counters are not shown as measurements', async () => {
    serve({ deps: () => HttpResponse.json(DB_DISABLED) })
    await openPage()
    await waitFor(() => expect(state('overall-state')).toBe('healthy'))
    expect(state('dep-database')).toBe('disabled')
    expect(screen.getByTestId('dep-persistence')).toHaveTextContent(/Not applicable.*defaults, not measurements/)
    expect(screen.getByTestId('dep-persistence')).not.toHaveTextContent(/Written/)
    expect(screen.getByTestId('dep-health-backend')).toHaveTextContent(/memory.*Per process/)
  })

  it('unavailable: GET /health does not answer', async () => {
    server.use(http.get('/health', () => HttpResponse.error()))
    serve({ deps: () => HttpResponse.error(), metrics: () => HttpResponse.error() })
    await openPage()
    await waitFor(() => expect(state('overall-state')).toBe('unavailable'))
    expect(state('dep-api')).toBe('unavailable')
    expect((await screen.findAllByText('Backend unreachable')).length).toBeGreaterThan(0)
  })

  it('a malformed dependency body is reported, never rendered as partial health', async () => {
    serve({ deps: () => HttpResponse.json({ redis: 'ok' }) })
    await openPage()
    expect(await screen.findByTestId('malformed')).toHaveTextContent(/does not match the dependency schema/)
    expect(screen.queryByTestId('dep-redis')).not.toBeInTheDocument()
    expect(state('overall-state')).toBe('unknown')
  })
})

describe('Observability: metrics', () => {
  it('renders counters and gauges from the real exposition, with units', async () => {
    serve()
    await openPage()
    expect(await kpi('HTTP requests')).toHaveTextContent('16')
    expect(await kpi('In flight')).toHaveTextContent('1')
    expect(await kpi('HTTP errors')).toHaveTextContent('8')
    expect(await kpi('Chat requests')).toHaveTextContent('4')
    expect(await kpi('Provider attempts')).toHaveTextContent('5')
    expect(await kpi('Cache hit ratio')).toHaveTextContent('25.0%')
    expect(await kpi('Tokens')).toHaveTextContent('204')
    expect(await kpi('Estimated cost')).toHaveTextContent('$0.000241')
    expect(screen.getByText('Placeholder pricing: not real spend')).toBeInTheDocument()
    expect(screen.getByTestId('placeholder-pricing')).toHaveTextContent(/not real spend/)
    expect(screen.getByText('v0.1.0 · mock mode true')).toBeInTheDocument()
    expect(screen.getByTestId('status-2xx')).toHaveTextContent('8')
    expect(screen.getByTestId('status-4xx')).toHaveTextContent('8')
    const errors = screen.getByRole('list', { name: 'Errors by category and code' })
    expect(within(errors).getAllByRole('listitem').map((li) => li.textContent)).toEqual(['validation · validation_error6', 'auth · unauthorized1', 'client · unknown_model1'])
  })

  it('histograms: exact mean and the bucket that holds p50/p95 (seconds shown as ms/s)', async () => {
    serve()
    await openPage()
    const table = await screen.findByRole('table', { name: 'Requests by route' })
    const chatRow = within(table).getAllByRole('row').find((r) => r.textContent?.startsWith('/api/v1/chat'))!
    const cells = within(chatRow).getAllByRole('cell').map((c) => c.textContent)
    expect(cells).toEqual(['/api/v1/chat', 'POST', '11', '7', '0', '53 ms', '10 ms – 25 ms', '250 ms – 500 ms'])
    expect(screen.getByTestId('http-panel')).toHaveTextContent(/they are ranges, not measurements/)
  })

  it('provider attempts separate success, error and skipped; retries and fallbacks are listed', async () => {
    serve()
    await openPage()
    const table = await screen.findByRole('table', { name: 'Provider attempts by model' })
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(
      ['Model', 'Provider', 'Success', 'Error', 'Skipped: unhealthy', 'Skipped: rate limit', 'Skipped: budget'])
    const adv = within(table).getAllByRole('row').find((r) => r.textContent?.startsWith('mock-advanced'))!
    expect(within(adv).getAllByRole('cell').map((c) => c.textContent)).toEqual(['mock-advanced', 'mock', '0', '2', '0', '0', '0'])
    expect(screen.getByRole('list', { name: 'Failed attempts by error kind' })).toHaveTextContent('unavailable2')
    expect(screen.getByRole('list', { name: 'Retries by model' })).toHaveTextContent('mock-advanced1')
    expect(screen.getByRole('list', { name: 'Fallbacks' })).toHaveTextContent(/mock-advanced.*mock-balanced1/)
    const latency = screen.getByRole('table', { name: 'Provider latency' })
    expect(within(latency).getByText('error')).toBeInTheDocument()
  })

  it('missing metrics are never zeros: not exposed, none recorded, real 0 and undefined ratio', async () => {
    serve({ metrics: () => new HttpResponse(SPARSE_METRICS, { headers: TEXT }) })
    await openPage()
    expect(await kpi('HTTP requests')).toHaveTextContent('None recorded') // family exposed, no series yet
    expect(await kpi('HTTP errors')).toHaveTextContent('Not exposed') // family absent
    expect(await kpi('Chat requests')).toHaveTextContent('Not exposed')
    expect(await kpi('Tokens')).toHaveTextContent('Not exposed')
    expect(await kpi('Estimated cost')).toHaveTextContent('Not exposed')
    expect(await kpi('In flight')).toHaveTextContent('1')
    expect(await kpi('Cache hit ratio')).toHaveTextContent('Undefined')
    expect(screen.getByText('No hits or misses yet')).toBeInTheDocument()
    expect(screen.getByTestId('embedding-cost')).toHaveTextContent('$0.00') // an actual 0 sample
    for (const label of ['HTTP requests', 'HTTP errors', 'Chat requests', 'Provider attempts', 'Tokens', 'Estimated cost']) {
      expect(screen.getByTestId(`kpi-${label}`).textContent).not.toMatch(/^\$?0/)
    }
    expect(screen.getByTestId('routing-panel').querySelectorAll('[data-absent="not_exposed"]').length).toBeGreaterThan(0)
    expect(screen.getByTestId('http-panel').querySelector('[data-absent="no_samples"]')).toHaveTextContent('None recorded: no HTTP requests.')
  })

  it('metrics disabled (404) is explained; dependency health is unaffected', async () => {
    serve({ metrics: () => errorResponse(404, 'not_found', 'Not Found') })
    await openPage()
    expect(await screen.findByTestId('metrics-disabled')).toHaveTextContent(/METRICS_ENABLED=false/)
    await waitFor(() => expect(state('overall-state')).toBe('healthy'))
    expect(screen.queryByTestId('metrics-kpis')).not.toBeInTheDocument()
  })

  it('a non-router or non-text body is "Unexpected response", not empty metrics', async () => {
    serve({ metrics: () => new HttpResponse('<html><body>proxy page</body></html>', { headers: { 'Content-Type': 'text/html' } }) })
    await openPage()
    expect(await screen.findByTestId('malformed')).toHaveTextContent(/contained no llm_router_\* metrics/)
    expect(screen.queryByTestId('metrics-kpis')).not.toBeInTheDocument()
  })

  it('lists the exposed families and explains interpretation and tracing', async () => {
    serve()
    await openPage()
    await kpi('HTTP requests')
    expect(screen.getByTestId('families')).toHaveTextContent(/Exposed metric families \(18\)/)
    expect(screen.getByTestId('process-note')).toHaveTextContent(/cumulative since the backend process.*resets when it restarts.*not cluster totals.*nothing polls/)
    expect(screen.getByTestId('interpretation')).toHaveTextContent(/Missing is not zero.*not an invoice/)
    expect(screen.getByTestId('tracing-status')).toHaveTextContent(/carried none: tracing is disabled, or those requests were not sampled/)
  })

  it('shows tracing as active only when the backend returned an X-Trace-ID', async () => {
    serve({ metrics: () => new HttpResponse(REAL_METRICS, { headers: { ...TEXT, 'X-Trace-ID': '4bf92f3577b34da6a3ce929d0e0e4736' } }) })
    await openPage()
    await kpi('HTTP requests')
    expect(screen.getByTestId('tracing-status')).toHaveTextContent(/4bf92f3577b34da6a3ce929d0e0e4736.*spans are being recorded/)
  })
})

describe('Observability: refresh', () => {
  it('loads once on open, again only on Refresh, and never polls', async () => {
    // Capture every interval and every long timeout registered from BEFORE mount, then fire them all by hand:
    // a polling loop of any period would issue new requests here.
    const scheduled: (() => void)[] = []
    const realInterval = globalThis.setInterval
    const realTimeout = globalThis.setTimeout
    vi.spyOn(globalThis, 'setInterval').mockImplementation(((fn: () => void, ms?: number) => {
      scheduled.push(fn)
      return realInterval(() => {}, ms)
    }) as typeof setInterval)
    vi.spyOn(globalThis, 'setTimeout').mockImplementation(((fn: () => void, ms?: number, ...args: unknown[]) => {
      if ((ms ?? 0) >= 20_000) scheduled.push(fn) // beyond the 15 s request timeout: only a poll would wait this long
      return realTimeout(fn, ms, ...args)
    }) as typeof setTimeout)
    const calls = serve()
    const user = userEvent.setup()
    renderApp('/observability')
    await kpi('HTTP requests')
    expect(calls.deps).toHaveLength(1)
    expect(calls.metrics).toHaveLength(1)
    for (const fn of scheduled) fn()
    await new Promise((r) => realTimeout(r, 100))
    expect(calls.deps).toHaveLength(1)
    expect(calls.metrics).toHaveLength(1)
    vi.restoreAllMocks()
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(calls.metrics).toHaveLength(2))
    expect(calls.deps).toHaveLength(2)
    expect(await screen.findByTestId('refresh-status')).toHaveTextContent(/^Updated /)
  })

  it('a failed refresh keeps earlier data, marked stale, and does not retry on its own', async () => {
    const calls = serve({
      deps: (n) => (n === 1 ? HttpResponse.json(DEPENDENCIES_FIXTURE) : errorResponse(503, 'service_unavailable', 'down')),
      metrics: (n) => (n === 1 ? new HttpResponse(REAL_METRICS, { headers: TEXT }) : HttpResponse.error()),
    })
    const { user } = await openPage()
    await kpi('HTTP requests')
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('Refresh failed · showing earlier data')).toBeInTheDocument()
    const notices = await screen.findAllByTestId('stale-notice')
    expect(notices.map((n) => n.textContent)).toEqual([
      expect.stringMatching(/the service was unavailable.*earlier dependency health from .*Nothing is retried automatically.*HTTP 503/),
      expect.stringMatching(/the backend was unreachable.*earlier metrics from/),
    ])
    expect(await kpi('HTTP requests')).toHaveTextContent('16')
    expect(state('dep-redis')).toBe('healthy') // last known, shown
    expect(state('overall-state')).toBe('unknown') // ...but stale data never decides the overall status
    await new Promise((r) => setTimeout(r, 100))
    expect(calls.deps).toHaveLength(2)
    expect(calls.metrics).toHaveLength(2)
  })
})

describe('Observability: authentication and errors', () => {
  it('401 on both: one token prompt; the token goes in the header only, stays in memory, and is never shown', async () => {
    const ok = (auth: string | null) => auth === `Bearer ${TOKEN}`
    const calls = serve({
      deps: (_n, auth) => (ok(auth) ? HttpResponse.json(DEPENDENCIES_FIXTURE) : errorResponse(401, 'unauthorized', 'Missing or invalid observability token.')),
      metrics: (_n, auth) => (ok(auth) ? new HttpResponse(REAL_METRICS, { headers: TEXT }) : errorResponse(401, 'unauthorized', 'Missing or invalid observability token.')),
    })
    const { user } = await openPage()
    await screen.findByTestId('token-elsewhere')
    const prompts = screen.getAllByRole('group', { name: 'Observability token required' })
    expect(prompts).toHaveLength(1)
    await user.type(within(prompts[0]).getByPlaceholderText('Observability token'), TOKEN)
    await user.click(within(prompts[0]).getByRole('button', { name: 'Use token' }))
    expect(await kpi('HTTP requests')).toHaveTextContent('16')
    await waitFor(() => expect(state('overall-state')).toBe('healthy'))
    expect(calls.deps.at(-1)).toBe(`Bearer ${TOKEN}`)
    expect(calls.metrics.at(-1)).toBe(`Bearer ${TOKEN}`)
    for (const url of calls.urls) expect(url).not.toContain(TOKEN)
    expect(credentialStore.get('observability')).toBe(TOKEN) // memory (existing policy), not storage
    expectTokenNowhere()
  })

  it('a token rejected on refresh keeps earlier data and offers the prompt again', async () => {
    credentialStore.set('observability', TOKEN)
    serve({
      deps: (n) => (n === 1 ? HttpResponse.json(DEPENDENCIES_FIXTURE) : errorResponse(401, 'unauthorized', 'Missing or invalid observability token.')),
      metrics: (n) => (n === 1 ? new HttpResponse(REAL_METRICS, { headers: TEXT }) : errorResponse(401, 'unauthorized', 'Missing or invalid observability token.')),
    })
    const { user } = await openPage()
    await kpi('HTTP requests')
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByRole('group', { name: 'Observability token required' })).toHaveTextContent(/rejected the stored credential/)
    expect(screen.getAllByRole('group', { name: 'Observability token required' })).toHaveLength(1)
    expect((await screen.findAllByTestId('stale-notice'))[0]).toHaveTextContent(/observability token was rejected/)
    expect(await kpi('HTTP requests')).toHaveTextContent('16')
    expectTokenNowhere()
  })

  it.each([
    [429, 'rate_limit_exceeded', 'Rate limit reached', { 'Retry-After': '15' }],
    [503, 'service_unavailable', 'Service unavailable', undefined],
    [403, 'forbidden', 'Not allowed', undefined],
    [500, 'internal_error', 'Something went wrong', undefined],
  ])('metrics HTTP %s shows "%s" without a fake value', async (status, code, title, headers) => {
    serve({ metrics: () => errorResponse(status, code, `backend: ${code}`, { headers }) })
    await openPage()
    expect(await screen.findByText(title)).toBeInTheDocument()
    expect(screen.getByText(`backend: ${code}`)).toBeInTheDocument()
    expect(screen.getByText(`HTTP ${status} · ${code}`)).toBeInTheDocument()
    expect(screen.queryByTestId('metrics-kpis')).not.toBeInTheDocument()
  })

  it('network failure shows "Backend unreachable"', async () => {
    serve({ metrics: () => HttpResponse.error() })
    await openPage()
    expect((await screen.findAllByText('Backend unreachable')).length).toBeGreaterThan(0)
  })

  it.each([
    ['timeout', 'the request timed out'],
    ['rate_limited', 'the rate limit was reached (retry after 15s)'],
    ['forbidden', 'access was refused (HTTP 403)'],
  ] as const)('stale notice names a %s failure distinctly', (kind, text) => {
    const error = new ApiError({ message: 'x', status: null, code: kind, kind, retryAfterSeconds: kind === 'rate_limited' ? 15 : null })
    render(<SectionFailure error={error} hasData at={null} what="metrics" onRetry={() => {}} />)
    expect(screen.getByTestId('stale-notice')).toHaveTextContent(`Refresh failed: ${text}.`)
  })

  it('does not expose backend error details beyond the message and code', async () => {
    serve({ metrics: () => HttpResponse.json({ error: { code: 'internal_error', message: 'Internal server error', details: { trace: 'Traceback (most recent call last)' } } }, { status: 500 }) })
    await openPage()
    expect(await screen.findByText('Internal server error')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('Traceback')
  })
})

describe('Observability: layout', () => {
  it('uses responsive grids and scrollable tables', async () => {
    serve()
    await openPage()
    const grid = (await kpi('HTTP requests')).closest('.grid')!
    expect(grid.className).toContain('grid-cols-2')
    expect(grid.className).toContain('md:grid-cols-4')
    const table = screen.getByRole('table', { name: 'Requests by route' })
    expect(table.parentElement!.className).toContain('overflow-x-auto')
    const split = screen.getByTestId('usage-panel').parentElement!
    expect(split.className).toContain('grid-cols-1')
    expect(split.className).toContain('xl:grid-cols-2')
  })
})
