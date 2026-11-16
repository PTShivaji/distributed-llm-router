/**
 * Step 10.6 - cross-page integration (whole route table, shared shell, shared stores).
 * Mocked API only (MSW, unhandled requests fail): live-backend checks are done separately in the browser run.
 */
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { sessionActivity } from '../activity/sessionActivity'
import type { ChatResponse } from '../api/types'
import { credentialStore } from '../auth/credentials'
import { chatSession } from '../chat/chatSession'
import { NAV_ITEMS } from '../components/layout/navigation'
import { HEALTH_RECHECK_MS } from '../hooks/useBackendHealth'
import { AUTO_REFRESH_MS } from '../pages/OverviewPage'
import { CAPTURED, clone } from '../test/budgetFixtures'
import { REAL_METRICS } from '../test/metricsFixture'
import { renderApp } from '../test/render'
import { CACHE_FIXTURE, DEPENDENCIES_FIXTURE, EMPTY_SUMMARY, errorResponse, server } from '../test/server'

const OBS = 'obs-token-integration-0123456789AB'
const CHAT = 'chat-key-integration-0123456789AB'
const ADMIN = 'admin-token-integration-0123456789'

interface Req { method: string; path: string; auth: string | null; url: string }
let requests: Req[] = []

beforeEach(() => {
  requests = []
  server.events.on('request:start', ({ request }) => {
    const u = new URL(request.url)
    requests.push({ method: request.method, path: u.pathname, auth: request.headers.get('authorization'), url: request.url })
  })
})
afterEach(() => {
  server.events.removeAllListeners()
  vi.restoreAllMocks()
})

const key = (r: Req) => `${r.method} ${r.path}`
const counts = (list: Req[] = requests) => list.reduce<Record<string, number>>((m, r) => ({ ...m, [key(r)]: (m[key(r)] ?? 0) + 1 }), {})
const settle = () => act(() => new Promise((r) => setTimeout(r, 150)))

function chatResponse(answer: string): ChatResponse {
  return {
    answer, request_id: `req-${answer.length}`, mock_mode: true, latency_ms: 12, selected_model: 'mock-fast', model_used: 'mock-fast',
    provider: 'mock', provider_model: 'mock/fast-v1', provider_adapter: 'mock', provider_status: 'ok', finish_reason: 'stop',
    classification: {
      classifier_version: 'v', complexity: 'SIMPLE', complexity_score: 0.1, confidence: 0.9, explanation: '',
      features: { char_count: 5, code_line_count: 0, has_code: false, has_code_block: false, inline_code_count: 0, line_count: 1,
        list_item_count: 0, question_count: 1, sentence_count: 1, word_count: 1 },
      intent: { primary: 'question_answering', confidence: 0.8, alternatives: [] },
      reasoning: { estimated_steps: 1, score: 0.1, signals: [] },
      technical: { detected_terms: [], domains: [], is_coding: false, is_technical: false, score: 0 },
      thresholds: { simple_max: 0.3, complex_min: 0.6 },
    },
    routing: { selected_model: 'mock-fast', requested_tier: 'SIMPLE', routed_tier: 'SIMPLE', strategy: 'priority', override: false,
      reason: 'SIMPLE', candidates: ['mock-fast'], preferred_capabilities: [] },
    fallback: { used: false, chain: ['mock-fast'], original_model: 'mock-fast', final_model: 'mock-fast', reason: null,
      attempts: [{ attempt_number: 1, model_id: 'mock-fast', provider: 'mock', outcome: 'success', error_kind: null, retryable: null, latency_ms: 10 }] },
    usage: { input_tokens: 3, output_tokens: 5, total_tokens: 8, source: 'estimated' },
    cost: { currency: 'USD', input_cost: 0, output_cost: 0, total_cost: 0.000002, input_cost_per_million: 0.1, output_cost_per_million: 0.4,
      is_estimate: true, pricing_is_placeholder: true },
    cache: { status: 'miss', stored: true, lookup_ms: 1 },
  }
}

/** A backend with all three credentials configured, mirroring backend/app/api/deps.py. */
function protectedBackend() {
  const obs = (r: Request) => r.headers.get('authorization') === `Bearer ${OBS}`
  const unauthorized = (what: string) => errorResponse(401, 'unauthorized', `Missing or invalid ${what}.`)
  server.use(
    http.get('/api/v1/analytics/summary', ({ request }) => (obs(request) ? HttpResponse.json(EMPTY_SUMMARY) : unauthorized('observability token'))),
    http.get('/api/v1/cache/stats', ({ request }) => (obs(request) ? HttpResponse.json(CACHE_FIXTURE) : unauthorized('observability token'))),
    http.get('/api/v1/health/dependencies', ({ request }) => (obs(request) ? HttpResponse.json(DEPENDENCIES_FIXTURE) : unauthorized('observability token'))),
    http.get('/metrics', ({ request }) => (obs(request) ? new HttpResponse(REAL_METRICS, { headers: { 'Content-Type': 'text/plain' } }) : unauthorized('observability token'))),
    ...(['status', 'spend', 'refusals'] as const).map((name) => http.get(`/api/v1/budget/${name}`, ({ request }) =>
      (obs(request) ? HttpResponse.json(clone(CAPTURED[name])) : unauthorized('observability token')))),
    http.post('/api/v1/chat', async ({ request }) => {
      if (request.headers.get('authorization') !== `Bearer ${CHAT}`) return unauthorized('chat API key')
      const body = (await request.json()) as { query: string }
      return HttpResponse.json(chatResponse(`answer to ${body.query}`))
    }),
    http.delete('/api/v1/cache', ({ request }) => (request.headers.get('authorization') === `Bearer ${ADMIN}`
      ? HttpResponse.json({ cleared: true, deleted_keys: 5, reset_stats: false })
      : unauthorized('admin token'))),
  )
}

async function go(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(within(screen.getByRole('navigation')).getByRole('link', { name: label }))
  await screen.findByRole('heading', { level: 1, name: label })
}

/** Endpoints each page requests when it is opened (plus the shared, once-per-app GET /health). */
const PAGE_REQUESTS: Record<string, string[]> = {
  '/': ['GET /api/v1/analytics/summary', 'GET /api/v1/cache/stats', 'GET /api/v1/models'], // + 24 hourly summary windows (chart)
  '/chat': ['GET /api/v1/models'],
  '/models': ['GET /api/v1/models'],
  '/analytics': ['GET /api/v1/analytics/summary', 'GET /api/v1/models', 'GET /metrics'],
  '/budget': ['GET /api/v1/budget/status', 'GET /api/v1/budget/spend', 'GET /api/v1/budget/refusals'],
  '/cache': ['GET /api/v1/cache/stats'],
  '/observability': ['GET /api/v1/health/dependencies', 'GET /metrics'],
  '/settings': ['GET /api/v1/settings', 'GET /api/v1/health/dependencies'],
}

describe('routing: direct loads, refresh, history', () => {
  it.each(NAV_ITEMS.map((n) => [n.path, n.label]))('direct load of %s renders %s, marks it active and requests each endpoint exactly once', async (path, label) => {
    renderApp(path)
    expect(await screen.findByRole('heading', { level: 1, name: label })).toBeInTheDocument()
    expect(within(screen.getByRole('navigation')).getByRole('link', { name: label })).toHaveAttribute('aria-current', 'page')
    expect(within(screen.getByRole('banner')).getByText(label)).toBeInTheDocument()
    const expected = Object.fromEntries(['GET /health', ...PAGE_REQUESTS[path]].map((k) => [k, 1]))
    if (path === '/') expected['GET /api/v1/analytics/summary'] = 1 + 24 // KPIs + the 24 hourly windows of the activity chart
    await waitFor(() => expect(counts()).toEqual(expected))
    await settle()
    expect(counts()).toEqual(expected) // no duplicate or late requests caused by mounting
    expect(new Set(requests.map((r) => `${r.method} ${r.url}`)).size).toBe(requests.length) // no identical request twice
  })

  it.each(NAV_ITEMS.map((n) => [n.path, n.label]))('browser refresh on %s (a fresh mount) shows the same page again', async (path, label) => {
    const first = renderApp(path)
    await screen.findByRole('heading', { level: 1, name: label })
    first.unmount()
    renderApp(path)
    expect(await screen.findByRole('heading', { level: 1, name: label })).toBeInTheDocument()
  })

  it('back and forward restore the page, its active link and its data, with one request per mount', async () => {
    const user = userEvent.setup()
    const { router } = renderApp('/')
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    await go(user, 'Model Registry')
    await go(user, 'Cache Management')
    await act(() => router.navigate(-1))
    expect(await screen.findByRole('heading', { level: 1, name: 'Model Registry' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/models')
    expect(within(screen.getByRole('navigation')).getByRole('link', { name: 'Model Registry' })).toHaveAttribute('aria-current', 'page')
    await act(() => router.navigate(-1))
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument()
    await act(() => router.navigate(1))
    expect(await screen.findByRole('heading', { level: 1, name: 'Model Registry' })).toBeInTheDocument()
    await screen.findAllByText('mock-advanced')
    await settle()
    const c = counts()
    expect(c['GET /health']).toBe(1) // shared across the whole session, not per page
    expect(c['GET /api/v1/models']).toBe(5) // one per mount of a page that uses it: Overview, Registry, Registry (back), Overview (back), Registry (forward)
    expect(c['GET /api/v1/cache/stats']).toBe(3) // Overview x2 + Cache x1
  })

  it.each(['/does-not-exist', '/models/extra', '/chatter', '/cache/x/y'])('unknown route %s shows Not found and no page is labelled or highlighted', async (path) => {
    renderApp(path)
    expect(await screen.findByText('Page not found')).toBeInTheDocument()
    expect(within(screen.getByRole('banner')).getByText('Not found')).toBeInTheDocument()
    for (const link of within(screen.getByRole('navigation')).getAllByRole('link')) expect(link).not.toHaveAttribute('aria-current')
    await settle()
    expect(counts()).toEqual({ 'GET /health': 1 })
  })

  it('leaving the Overview stops its auto-refresh timer (no polling continues on other pages)', async () => {
    const created: { id: number; ms: number }[] = []
    const cleared = new Set<number>()
    const realSet = window.setInterval.bind(window)
    const realClear = window.clearInterval.bind(window)
    vi.spyOn(window, 'setInterval').mockImplementation(((fn: () => void, ms?: number) => {
      const id = realSet(fn, ms)
      if ((ms ?? 0) >= 10_000) created.push({ id: id as unknown as number, ms: ms ?? 0 })
      return id
    }) as typeof setInterval)
    vi.spyOn(window, 'clearInterval').mockImplementation((id?: number) => {
      if (id !== undefined) cleared.add(id)
      realClear(id)
    })
    const user = userEvent.setup()
    renderApp('/')
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    // Overview's approved, opt-out auto-refresh
    await waitFor(() => expect(created.some((t) => t.ms === AUTO_REFRESH_MS)).toBe(true))
    await go(user, 'Model Registry')
    for (const t of created.filter((x) => x.ms === AUTO_REFRESH_MS)) expect(cleared.has(t.id)).toBe(true)
    // The ONLY long-interval timer allowed to outlive the Overview is the app-wide /health re-check
    // of the top-bar connection badge (one timer, HEALTH_RECHECK_MS).
    const alive = created.filter((t) => !cleared.has(t.id))
    expect(alive.every((t) => t.ms === HEALTH_RECHECK_MS)).toBe(true)
    expect(alive.length).toBeLessThanOrEqual(1)
  })
})

describe('credentials across pages', () => {
  it('one observability token entered on one page serves every observability page; each credential reaches only its endpoints', async () => {
    protectedBackend()
    const user = userEvent.setup()
    renderApp('/')
    const prompt = (await screen.findAllByRole('group', { name: 'Observability token required' }))[0]
    await user.type(within(prompt).getByPlaceholderText('Observability token'), OBS)
    await user.click(within(prompt).getByRole('button', { name: 'Use token' }))
    await waitFor(() => expect(screen.queryByRole('group', { name: 'Observability token required' })).not.toBeInTheDocument())

    for (const label of ['Routing Analytics', 'Cache Management', 'Observability']) {
      await go(user, label)
      await settle()
      await waitFor(() => expect(screen.queryAllByRole('group', { name: 'Observability token required' })).toHaveLength(0))
    }
    await go(user, 'Chat Playground')
    await screen.findByRole('option', { name: /mock-fast · mock/ })
    await user.type(screen.getByLabelText('Prompt'), 'hello{Enter}')
    const error = await screen.findByTestId('chat-error')
    await user.type(within(error).getByPlaceholderText('Chat API key'), CHAT)
    await user.click(within(error).getByRole('button', { name: 'Use token' }))
    await settle()
    expect(requests.filter((r) => r.method === 'POST' && r.path === '/api/v1/chat')).toHaveLength(1) // saving does not submit
    await user.click(within(error).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('answer to hello')).toBeInTheDocument()

    const OBS_PATHS = ['/api/v1/analytics/summary', '/api/v1/cache/stats', '/api/v1/health/dependencies', '/metrics']
    for (const r of requests) {
      expect(r.url).not.toContain(OBS)
      expect(r.url).not.toContain(CHAT)
      if (r.auth === `Bearer ${OBS}`) expect(OBS_PATHS).toContain(r.path)
      if (r.auth === `Bearer ${CHAT}`) expect(r.path).toBe('/api/v1/chat')
      if (!OBS_PATHS.includes(r.path) && r.path !== '/api/v1/chat') expect(r.auth).toBeNull() // /health, /models, /classify: public
    }
    expect(requests.filter((r) => r.method === 'DELETE')).toHaveLength(0) // no destructive call happened on its own
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0) // nobody opted in
    expect(document.body.innerHTML).not.toContain(OBS)
    expect(document.body.innerHTML).not.toContain(CHAT)
  })

  it('the admin token is used for the DELETE only and appears on no later request, page or store', async () => {
    protectedBackend()
    credentialStore.set('observability', OBS)
    credentialStore.set('chat', CHAT)
    const user = userEvent.setup()
    renderApp('/cache')
    await screen.findByTestId('kpi-Exact hits')
    await user.click(screen.getByRole('button', { name: 'Clear cache…' }))
    const dialog = screen.getByRole('dialog', { name: 'Clear response cache' })
    await user.type(within(dialog).getByLabelText('Admin token'), ADMIN)
    await user.click(within(dialog).getByLabelText(/I understand/))
    await user.click(within(dialog).getByRole('button', { name: 'Clear cache' }))
    await screen.findByTestId('clear-result')
    const deleteIndex = requests.findIndex((r) => r.method === 'DELETE')
    expect(requests[deleteIndex].auth).toBe(`Bearer ${ADMIN}`)

    await go(user, 'Observability')
    await screen.findByTestId('metrics-kpis')
    await go(user, 'Chat Playground')
    await screen.findByRole('option', { name: /mock-fast · mock/ })
    await user.type(screen.getByLabelText('Prompt'), 'after clear{Enter}')
    await screen.findByText('answer to after clear')
    for (const r of requests.slice(deleteIndex + 1)) expect(r.auth ?? '').not.toContain(ADMIN)
    expect(credentialStore.get('observability')).toBe(OBS)
    expect(credentialStore.get('chat')).toBe(CHAT)
    expect(document.body.innerHTML).not.toContain(ADMIN)
    expect(JSON.stringify({ ...window.sessionStorage }) + JSON.stringify({ ...window.localStorage })).not.toContain(ADMIN)
  })

  it('clearing the chat key on the Chat page takes effect at once and leaves the observability token alone', async () => {
    protectedBackend()
    credentialStore.set('observability', OBS)
    credentialStore.set('chat', CHAT)
    const user = userEvent.setup()
    renderApp('/chat')
    await screen.findByRole('option', { name: /mock-fast · mock/ })
    await user.click(within(screen.getByTestId('chat-key-status')).getByRole('button', { name: 'Clear' }))
    await user.type(screen.getByLabelText('Prompt'), 'no key{Enter}')
    expect(await screen.findByTestId('chat-error')).toHaveTextContent('Chat API key required')
    expect(requests.filter((r) => r.path === '/api/v1/chat').at(-1)?.auth).toBeNull()
    await go(user, 'Observability')
    await screen.findByTestId('metrics-kpis')
    expect(requests.filter((r) => r.path === '/metrics').at(-1)?.auth).toBe(`Bearer ${OBS}`)
  })

  it('opting in keeps credentials in sessionStorage for the tab; opting out wipes them', async () => {
    protectedBackend()
    const user = userEvent.setup()
    renderApp('/observability')
    const prompt = await screen.findByRole('group', { name: 'Observability token required' })
    await user.type(within(prompt).getByPlaceholderText('Observability token'), OBS)
    await user.click(within(prompt).getByRole('checkbox'))
    await user.click(within(prompt).getByRole('button', { name: 'Use token' }))
    await screen.findByTestId('metrics-kpis')
    expect(Object.values({ ...window.sessionStorage })).toContain(OBS)
    expect(window.localStorage.length).toBe(0)
    act(() => credentialStore.setPersistent(false))
    expect(Object.values({ ...window.sessionStorage })).not.toContain(OBS)
    expect(credentialStore.get('observability')).toBe(OBS) // still usable in memory
  })
})

describe('cross-page state', () => {
  it('a chat shows up in Overview recent activity; clearing the cache keeps the conversation and activity', async () => {
    protectedBackend()
    credentialStore.set('observability', OBS)
    credentialStore.set('chat', CHAT)
    const user = userEvent.setup()
    renderApp('/chat')
    await screen.findByRole('option', { name: /mock-fast · mock/ })
    await user.type(screen.getByLabelText('Prompt'), 'capital of France{Enter}')
    await screen.findByText('answer to capital of France')

    await go(user, 'Overview')
    const activity = screen.getByRole('region', { name: 'Recent activity' })
    expect(await within(activity).findByText('mock-fast')).toBeInTheDocument()
    expect(activity).not.toHaveTextContent('capital of France') // metadata only

    await go(user, 'Cache Management')
    await screen.findByTestId('kpi-Exact hits')
    await user.click(screen.getByRole('button', { name: 'Clear cache…' }))
    const dialog = screen.getByRole('dialog', { name: 'Clear response cache' })
    await user.type(within(dialog).getByLabelText('Admin token'), ADMIN)
    await user.click(within(dialog).getByLabelText(/I understand/))
    await user.click(within(dialog).getByRole('button', { name: 'Clear cache' }))
    await screen.findByTestId('clear-result')
    expect(chatSession.list()).toHaveLength(1)
    expect(sessionActivity.list()).toHaveLength(1)

    await go(user, 'Chat Playground')
    expect(screen.getByText('answer to capital of France')).toBeInTheDocument()
    await go(user, 'Overview')
    expect(await within(screen.getByRole('region', { name: 'Recent activity' })).findByText('mock-fast')).toBeInTheDocument()
  })

  it.each([
    ['/cache', 'Cache Management', ['GET /api/v1/cache/stats', 'GET /health']],
    ['/observability', 'Observability', ['GET /api/v1/health/dependencies', 'GET /metrics', 'GET /health']],
    ['/analytics', 'Routing Analytics', ['GET /api/v1/analytics/summary', 'GET /api/v1/models', 'GET /metrics', 'GET /health']],
    ['/models', 'Model Registry', ['GET /api/v1/models', 'GET /health']],
    ['/', 'Overview', ['GET /api/v1/analytics/summary', 'GET /api/v1/cache/stats', 'GET /api/v1/models', 'GET /health']],
  ])('Refresh on %s reloads only that page\'s data (and the shared health check)', async (path, label, allowed) => {
    const user = userEvent.setup()
    renderApp(path)
    await screen.findByRole('heading', { level: 1, name: label })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).not.toBeDisabled())
    await settle()
    const before = requests.length
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await settle()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).not.toBeDisabled())
    const after = requests.slice(before)
    expect(after.length).toBeGreaterThan(0)
    for (const r of after) expect(allowed).toContain(key(r))
    const urls = after.map((r) => `${r.method} ${r.url}`)
    expect(new Set(urls).size).toBe(urls.length) // no identical request twice (the Overview chart's 24 hourly windows differ)
  })
})

describe('consistent failure handling across pages', () => {
  const PAGES = NAV_ITEMS.filter((n) => PAGE_REQUESTS[n.path].length > 0).map((n) => [n.path, n.label])

  it.each(PAGES)('backend down on %s (%s): "Backend unreachable", no fabricated values, nothing retried', async (path, label) => {
    server.use(http.all('*', () => HttpResponse.error()))
    renderApp(path)
    await screen.findByRole('heading', { level: 1, name: label })
    expect((await screen.findAllByText('Backend unreachable')).length).toBeGreaterThan(0)
    await settle()
    const urls = requests.map((r) => `${r.method} ${r.url}`)
    expect(new Set(urls).size).toBe(urls.length) // nothing retried (Overview's hourly chart stops at its first failed window)
    for (const el of screen.queryAllByTestId(/^kpi-/)) expect(el.textContent).not.toMatch(/^\$?0(\.0+)?%?$/)
  })

  it.each(PAGES.filter(([p]) => p !== '/chat' && p !== '/models'))('401 on %s (%s) offers the same observability-token prompt', async (path, label) => {
    protectedBackend()
    renderApp(path)
    await screen.findByRole('heading', { level: 1, name: label })
    expect((await screen.findAllByRole('group', { name: 'Observability token required' })).length).toBeGreaterThan(0)
  })
})
