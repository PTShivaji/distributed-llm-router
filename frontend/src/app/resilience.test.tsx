/**
 * Phase B (P1/P2) frontend resilience:
 *   - Retry-After honoured on 503 as well as 429; "busy, retry shortly" wording
 *   - /health re-checked every 30 s while visible, on becoming visible, and after network errors
 *   - unreachable-backend message built from the configured API base URL, no hard-coded address
 *   - mobile navigation drawer: inert while closed, Escape closes, focus in and back out
 *   - reload recovery for pages whose code could not be loaded; router-level error page
 */

import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createApiClient, onNetworkError } from '../api/client'
import { api } from '../api/endpoints'
import { ApiError, apiTarget, errorHint, isLocalTarget, networkMessage } from '../api/errors'
import type { CacheStatsResponse } from '../api/types'
import { CacheLookupSplit } from '../components/cache/CacheLookupSplit'
import { ErrorBoundary } from '../components/feedback/ErrorBoundary'
import { Dialog } from '../components/ui/Dialog'
import { Drawer } from '../components/ui/Drawer'
import { lookupSplit } from '../lib/cache'
import { HEALTH_RECHECK_MS, NETWORK_RECHECK_MIN_GAP_MS, useBackendHealth } from '../hooks/useBackendHealth'
import { describeChatError, rateLimitSummary } from '../lib/chat'
import { isChunkLoadError, pageReload } from '../lib/recovery'
import { RouteErrorPage } from '../pages/RouteErrorPage'
import { renderApp } from '../test/render'
import { CACHE_FIXTURE, errorResponse, HEALTH_FIXTURE, server } from '../test/server'

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

/* ------------------------------------------------------------------------------------------- */
describe('Retry-After on 429 and 503', () => {
  const client = () => createApiClient({ baseURL: '' })

  it('503 with Retry-After carries retryAfterSeconds (rate limiter busy)', async () => {
    server.use(http.get('/api/v1/models', () => errorResponse(503, 'rate_limiter_unavailable', 'Rate limiter busy; retry shortly.',
      { headers: { 'Retry-After': '1' } })))
    const error = await client().request({ url: '/api/v1/models' }).catch((e: unknown) => e)
    expect(error).toMatchObject({ status: 503, kind: 'unavailable', code: 'rate_limiter_unavailable', retryAfterSeconds: 1 })
  })

  it('503 without Retry-After has none; other statuses never take one', async () => {
    server.use(
      http.get('/a', () => errorResponse(503, 'database_unavailable', 'db down')),
      http.get('/b', () => errorResponse(500, 'internal_error', 'boom', { headers: { 'Retry-After': '9' } })),
    )
    expect(await client().request({ url: '/a' }).catch((e: unknown) => e)).toMatchObject({ status: 503, retryAfterSeconds: null })
    expect(await client().request({ url: '/b' }).catch((e: unknown) => e)).toMatchObject({ status: 500, retryAfterSeconds: null })
  })

  it('429 behaviour is unchanged', async () => {
    server.use(http.get('/c', () => errorResponse(429, 'rate_limit_exceeded', 'slow', { headers: { 'Retry-After': '12' } })))
    expect(await client().request({ url: '/c' }).catch((e: unknown) => e)).toMatchObject({ kind: 'rate_limited', retryAfterSeconds: 12 })
  })

  it('busy wording: hint, chat title and shared error title', () => {
    const busy = new ApiError({ message: 'Rate limiter busy; retry shortly.', status: 503, code: 'rate_limiter_unavailable',
      kind: 'unavailable', retryAfterSeconds: 2 })
    expect(errorHint(busy)).toBe('The backend is busy. Try again in 2s.')
    expect(errorHint(new ApiError({ ...busy, message: 'x', retryAfterSeconds: null }))).toBe('The backend is busy. Try again shortly.')
    expect(describeChatError(busy).title).toBe('Backend busy, retry shortly')
    expect(describeChatError(busy).explanation).not.toMatch(/could not check/)
    const generic503 = new ApiError({ message: 'x', status: 503, code: 'all_models_failed', kind: 'unavailable', retryAfterSeconds: 4 })
    expect(errorHint(generic503)).toBe('Try again in 4s.')
  })
})

/* ------------------------------------------------------------------------------------------- */
describe('unreachable-backend message uses the configured API base URL', () => {
  it('names the configured base URL, or this page origin when same-origin', () => {
    expect(networkMessage('https://router.example.com')).toBe('Cannot reach the backend API at https://router.example.com.')
    expect(apiTarget('')).toBe(window.location.origin)
    expect(networkMessage('')).toBe(`Cannot reach the backend API at ${window.location.origin}.`)
    expect(networkMessage('https://router.example.com')).not.toMatch(/127\.0\.0\.1:8000/)
  })

  it('development start-up hint only for a local API target', () => {
    expect(isLocalTarget('http://127.0.0.1:8000')).toBe(true)
    expect(isLocalTarget('http://localhost:5173')).toBe(true)
    expect(isLocalTarget('https://router.example.com')).toBe(false)
    expect(isLocalTarget('not a url')).toBe(false)
  })

  it('a real network failure reports the target, not a hard-coded address', async () => {
    server.use(http.get('/api/v1/models', () => HttpResponse.error()))
    const error = await api.models().catch((e: unknown) => e)
    expect(error).toMatchObject({ kind: 'network', message: `Cannot reach the backend API at ${window.location.origin}.` })
  })
})

/* ------------------------------------------------------------------------------------------- */
function HealthProbe() {
  const h = useBackendHealth()
  return <p data-testid="health">{h.status}</p>
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
  document.dispatchEvent(new Event('visibilitychange'))
}

describe('backend health is kept current', () => {
  afterEach(() => setVisibility('visible'))

  it('re-checks every 30 s while visible, skips hidden tabs, re-checks on becoming visible, stops when unused', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const health = vi.spyOn(api, 'health').mockResolvedValue({ data: HEALTH_FIXTURE, meta: { status: 200, requestId: null, traceId: null, rateLimit: null } })
    const { unmount } = render(<HealthProbe />)
    await waitFor(() => expect(screen.getByTestId('health')).toHaveTextContent('connected'))
    expect(health).toHaveBeenCalledTimes(1)
    expect(HEALTH_RECHECK_MS).toBe(30_000)

    await act(async () => { vi.advanceTimersByTime(HEALTH_RECHECK_MS) })
    expect(health).toHaveBeenCalledTimes(2)

    setVisibility('hidden')
    await act(async () => { vi.advanceTimersByTime(HEALTH_RECHECK_MS * 2) })
    expect(health).toHaveBeenCalledTimes(2) // hidden tab: nothing sent

    await act(async () => { setVisibility('visible') })
    expect(health).toHaveBeenCalledTimes(3) // immediate re-check on return

    unmount()
    await act(async () => { vi.advanceTimersByTime(HEALTH_RECHECK_MS * 3) })
    expect(health).toHaveBeenCalledTimes(3) // no subscribers: no polling
  })

  it('the badge turns "unreachable" after a later failure (no stale "Connected")', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    render(<HealthProbe />)
    await waitFor(() => expect(screen.getByTestId('health')).toHaveTextContent('connected'))
    server.use(http.get('/health', () => HttpResponse.error()))
    await act(async () => { vi.advanceTimersByTime(HEALTH_RECHECK_MS) })
    await waitFor(() => expect(screen.getByTestId('health')).toHaveTextContent('unreachable'))
  })

  it('a network error on any request triggers a prompt, debounced re-check', async () => {
    let now = 1_000_000
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    const health = vi.spyOn(api, 'health').mockResolvedValue({ data: HEALTH_FIXTURE, meta: { status: 200, requestId: null, traceId: null, rateLimit: null } })
    render(<HealthProbe />)
    await waitFor(() => expect(health).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('health')).toHaveTextContent('connected'))
    server.use(http.get('/api/v1/models', () => HttpResponse.error()))

    await api.models().catch(() => undefined) // within the debounce gap of the first check
    expect(health).toHaveBeenCalledTimes(1)

    now += NETWORK_RECHECK_MIN_GAP_MS + 1
    await api.models().catch(() => undefined)
    await waitFor(() => expect(health).toHaveBeenCalledTimes(2))
    await api.models().catch(() => undefined) // debounced again
    expect(health).toHaveBeenCalledTimes(2)
  })

  it('onNetworkError fires only for network failures, and listeners cannot break the request', async () => {
    const seen: string[] = []
    const off = onNetworkError(() => {
      seen.push('network')
      throw new Error('listener bug')
    })
    server.use(
      http.get('/down', () => HttpResponse.error()),
      http.get('/err', () => errorResponse(503, 'x', 'y')),
    )
    const client = createApiClient({ baseURL: '' })
    expect(await client.request({ url: '/down' }).catch((e: unknown) => e)).toMatchObject({ kind: 'network' })
    expect(await client.request({ url: '/err' }).catch((e: unknown) => e)).toMatchObject({ kind: 'unavailable' })
    expect(seen).toEqual(['network'])
    off()
  })
})

/* ------------------------------------------------------------------------------------------- */
function stubViewport(desktop: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: desktop, media: query, onchange: null,
    addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }))
}

describe('mobile navigation drawer accessibility', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('is inert while closed; opening moves focus in; Escape closes it and returns focus to the menu button', async () => {
    stubViewport(false)
    const user = userEvent.setup()
    renderApp('/models')
    const sidebar = await screen.findByRole('complementary', { name: 'Main navigation', hidden: true })
    expect(sidebar).toHaveAttribute('inert')
    const menu = screen.getByRole('button', { name: 'Open navigation' })

    await user.click(menu)
    expect(sidebar).not.toHaveAttribute('inert')
    expect(sidebar).toHaveAttribute('data-open', 'true')
    expect(within(sidebar).getByRole('button', { name: 'Close navigation' })).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(sidebar).toHaveAttribute('data-open', 'false')
    expect(sidebar).toHaveAttribute('inert')
    expect(menu).toHaveFocus()
  })

  it('choosing a link closes the drawer and focus does not get lost inside the inert drawer', async () => {
    stubViewport(false)
    const user = userEvent.setup()
    renderApp('/models')
    const sidebar = await screen.findByRole('complementary', { name: 'Main navigation', hidden: true })
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.click(within(sidebar).getByRole('link', { name: 'Chat Playground' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Chat Playground' })).toBeInTheDocument()
    expect(sidebar).toHaveAttribute('inert')
    expect(sidebar.contains(document.activeElement)).toBe(false)
  })

  it('Escape does nothing while the drawer is closed, and the desktop sidebar is never inert', async () => {
    stubViewport(true)
    const user = userEvent.setup()
    renderApp('/models')
    const sidebar = await screen.findByRole('complementary', { name: 'Main navigation' })
    expect(sidebar).not.toHaveAttribute('inert')
    await user.keyboard('{Escape}')
    expect(sidebar).not.toHaveAttribute('inert')
    expect(within(sidebar).getByRole('link', { name: 'Overview' })).toBeVisible()
  })
})

/* ------------------------------------------------------------------------------------------- */
function Boom({ error }: { error: Error }): never {
  throw error
}

describe('recovery from failed pages', () => {
  it('recognises lazy-chunk load failures from every major browser', () => {
    for (const message of [
      'Failed to fetch dynamically imported module: https://x/assets/OverviewPage-abc.js',
      'error loading dynamically imported module: https://x/assets/a.js',
      'Importing a module script failed.',
      'Unable to preload CSS for /assets/a.css',
    ]) expect(isChunkLoadError(new Error(message))).toBe(true)
    expect(isChunkLoadError(Object.assign(new Error('x'), { name: 'ChunkLoadError' }))).toBe(true)
    expect(isChunkLoadError(new Error('Cannot read properties of undefined'))).toBe(false)
    expect(isChunkLoadError('Failed to fetch dynamically imported module')).toBe(false)
  })

  it('a page whose code failed to load offers only "Reload page" (Try again cannot recover a cached failed import)', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const reload = vi.spyOn(pageReload, 'reload').mockImplementation(() => {})
    const user = userEvent.setup()
    render(<ErrorBoundary><Boom error={new Error('Failed to fetch dynamically imported module: /assets/x.js')} /></ErrorBoundary>)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('This page could not be loaded')
    expect(within(alert).queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
    await user.click(within(alert).getByRole('button', { name: 'Reload page' }))
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('an ordinary render error keeps "Try again" and adds "Reload page"', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const reload = vi.spyOn(pageReload, 'reload').mockImplementation(() => {})
    const user = userEvent.setup()
    render(<ErrorBoundary><Boom error={new TypeError('x is undefined')} /></ErrorBoundary>)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('This view failed to render')
    expect(within(alert).getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    await user.click(within(alert).getByRole('button', { name: 'Reload page' }))
    expect(reload).toHaveBeenCalledTimes(1)
  })

  it('router error page: real errors offer Overview + reload; error details are not dumped', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const reload = vi.spyOn(pageReload, 'reload').mockImplementation(() => {})
    const user = userEvent.setup()
    const router = createMemoryRouter([
      { path: '/', element: <p>home</p>, errorElement: <RouteErrorPage /> },
      { path: '/broken', element: <Boom error={new Error('secret-ish user input')} />, errorElement: <RouteErrorPage /> },
    ], { initialEntries: ['/broken'] })
    render(<RouterProvider router={router} />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Something went wrong')
    expect(alert).not.toHaveTextContent('secret-ish user input')
    await user.click(within(alert).getByRole('button', { name: 'Reload page' }))
    expect(reload).toHaveBeenCalledTimes(1)
    await user.click(within(alert).getByRole('link', { name: 'Go to Overview' }))
    expect(await screen.findByText('home')).toBeInTheDocument()
  })

  it('router error page: a 404 route response reads "Page not found" without a reload button', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    const router = createMemoryRouter([
      { path: '/', element: <p>home</p>, errorElement: <RouteErrorPage />,
        loader: () => { throw new Response('', { status: 404, statusText: 'Not Found' }) } },
    ], { initialEntries: ['/'] })
    render(<RouterProvider router={router} />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Page not found')
    expect(within(alert).queryByRole('button', { name: 'Reload page' })).not.toBeInTheDocument()
  })

  it('the app route table uses the router error page', async () => {
    const { routes } = await import('../router')
    const root = routes[0]
    expect(root.errorElement).toBeTruthy()
    expect((root.errorElement as { type: unknown }).type).toBe(RouteErrorPage)
  })
})

/* ------------------------------------------------------------------------------------------- */
describe('rate-limit quota summary', () => {
  it('formats the X-RateLimit headers and is absent without them', () => {
    expect(rateLimitSummary({ limit: 60, remaining: 57, resetSeconds: 42, window: 'minute' }))
      .toBe('57 of 60 requests left this minute, resets in 42s')
    expect(rateLimitSummary({ limit: 1000, remaining: 0, resetSeconds: null, window: 'hour' })).toBe('0 of 1,000 requests left this hour')
    expect(rateLimitSummary({ limit: 60, remaining: null, resetSeconds: null, window: null })).toBe('unknown of 60 requests left')
    expect(rateLimitSummary(null)).toBeNull()
    expect(rateLimitSummary({ limit: null, remaining: 5, resetSeconds: 1, window: 'minute' })).toBeNull()
  })
})

/* ------------------------------------------------------------------------------------------- */
describe('P3: dialogs keep keyboard focus inside', () => {
  it('Dialog: Tab wraps last -> first, Shift+Tab wraps first -> last; Escape still closes', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(
      <>
        <button type="button">outside</button>
        <Dialog open title="Confirm" onClose={onClose} footer={<><button type="button">Cancel</button><button type="button">OK</button></>} />
      </>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Confirm' })
    const close = within(dialog).getByRole('button', { name: 'Close dialog' })
    const ok = within(dialog).getByRole('button', { name: 'OK' })
    expect(close).toHaveFocus()
    await user.tab()
    await user.tab()
    expect(ok).toHaveFocus()
    await user.tab()
    expect(close).toHaveFocus() // wrapped, never reached "outside"
    await user.tab({ shift: true })
    expect(ok).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Drawer: focus starts on the close button and cannot leave the panel', async () => {
    const user = userEvent.setup()
    render(
      <>
        <button type="button">outside</button>
        <Drawer open title="model-x" onClose={() => {}}><a href="#detail">Details link</a></Drawer>
      </>,
    )
    const panel = screen.getByRole('dialog', { name: 'model-x' })
    const close = within(panel).getByRole('button', { name: 'Close details' })
    const link = within(panel).getByRole('link', { name: 'Details link' })
    expect(close).toHaveFocus()
    await user.tab()
    expect(link).toHaveFocus()
    await user.tab()
    expect(close).toHaveFocus()
    await user.tab({ shift: true })
    expect(link).toHaveFocus()
  })
})

/* ------------------------------------------------------------------------------------------- */
describe('P3: cache lookup split (only from measured backend counters)', () => {
  const stats = (o: Partial<CacheStatsResponse>): CacheStatsResponse => ({ ...CACHE_FIXTURE, ...o })

  it('splits measured lookups into exact hits, semantic hits and misses; a zero part draws nothing', () => {
    render(<CacheLookupSplit stats={stats({ hits_exact: 30, hits_semantic: 0, misses: 70, hits: 30 })} />)
    const bar = screen.getByRole('img')
    expect(bar).toHaveAccessibleName('Cache lookups: Exact hits 30 (30.0%), Semantic hits 0 (0.0%), Misses 70 (70.0%), of 100 lookups')
    expect(screen.getByTestId('lookup-split-exact')).toHaveStyle({ flexGrow: '30' })
    expect(screen.getByTestId('lookup-split-miss')).toHaveStyle({ flexGrow: '70' })
    expect(screen.queryByTestId('lookup-split-semantic')).not.toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Legend' })).getAllByRole('listitem').map((li) => li.textContent))
      .toEqual(['Exact hits', 'Semantic hits', 'Misses']) // names only, in block order
  })

  it('shows nothing when the counters are defaults (cache disabled or Redis unread), and a note when there are no lookups', () => {
    const { container, rerender } = render(<CacheLookupSplit stats={stats({ available: false, hits_exact: 5, misses: 5 })} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<CacheLookupSplit stats={stats({ enabled: false, hits_exact: 5, misses: 5 })} />)
    expect(container).toBeEmptyDOMElement()
    rerender(<CacheLookupSplit stats={stats({})} />)
    expect(screen.getByTestId('lookup-split-empty')).toHaveTextContent('No cache lookups yet')
    expect(lookupSplit(stats({ available: false }))).toBeNull()
  })

  it('appears on the Cache Management page under the statistics', async () => {
    server.use(http.get('/api/v1/cache/stats', () => HttpResponse.json(stats({ hits_exact: 3, hits_semantic: 1, misses: 4, hits: 4, hit_rate: 0.5 }))))
    renderApp('/cache')
    expect(await screen.findByTestId('lookup-split')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: /Exact hits 3 \(37\.5%\), Semantic hits 1 \(12\.5%\), Misses 4 \(50\.0%\), of 8 lookups/ })).toBeInTheDocument()
  })
})
