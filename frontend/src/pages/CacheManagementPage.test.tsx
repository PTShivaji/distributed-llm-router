import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CacheStatsResponse } from '../api/types'
import { credentialStore } from '../auth/credentials'
import { renderApp } from '../test/render'
import { CACHE_FIXTURE, errorResponse, server } from '../test/server'

/* Mocked API data only. */
const STATS: CacheStatsResponse = {
  ...CACHE_FIXTURE,
  enabled: true, semantic_enabled: false, available: true, embedding_provider: 'local-hashing-v1-256', embedding_is_semantic: false,
  similarity_threshold: 0.92, ttl_seconds: 3600, total_entries: 12, scopes: 2, hits: 7, hits_exact: 7, hits_semantic: 0, misses: 13,
  stores: 13, hit_rate: 0.35, approx_storage_bytes: 20_480,
  notes: [
    'Local hashing embedder: similarity is lexical (shared words), not meaning-aware.',
    'Approximate (semantic) hits are DISABLED (CACHE_SEMANTIC_ENABLED=false, the default). Exact-match caching is active.',
    'approx_storage_bytes is an estimate from Redis MEMORY USAGE sampling.',
  ],
}

/** What the backend returns when Redis cannot be read: counters are 0 DEFAULTS, not measurements. */
const REDIS_DOWN: CacheStatsResponse = {
  ...STATS, available: false, total_entries: null, scopes: null, approx_storage_bytes: null, hits: 0, hits_exact: 0, hits_semantic: 0,
  misses: 0, stores: 0, hit_rate: null, notes: [STATS.notes[0], 'Redis unavailable (ConnectionError); counts unknown.'],
}

/** Exact shape of the backend's "cache disabled" response (backend/app/api/cache.py). */
const DISABLED: CacheStatsResponse = {
  enabled: false, semantic_enabled: false, available: false, embedding_provider: null, embedding_is_semantic: false,
  similarity_threshold: null, ttl_seconds: null, total_entries: null, scopes: null, hits: 0, hits_exact: 0, hits_semantic: 0, misses: 0,
  stores: 0, hit_rate: null, approx_storage_bytes: null, notes: ['Cache disabled (CACHE_ENABLED=false or REDIS_ENABLED=false).'],
}

const ADMIN = 'admin-token-page-tests-ABCDEFGHIJ'

function serveStats(respond: (n: number, auth: string | null) => Response = () => HttpResponse.json(STATS)) {
  const calls: (string | null)[] = []
  server.use(http.get('/api/v1/cache/stats', ({ request }) => {
    calls.push(request.headers.get('authorization'))
    return respond(calls.length, request.headers.get('authorization'))
  }))
  return calls
}

function serveClear(respond: () => Response) {
  const calls: { auth: string | null; resetStats: string | null }[] = []
  server.use(http.delete('/api/v1/cache', ({ request }) => {
    calls.push({ auth: request.headers.get('authorization'), resetStats: new URL(request.url).searchParams.get('reset_stats') })
    return respond()
  }))
  return calls
}

const kpi = (label: string) => screen.findByTestId(`kpi-${label}`)

async function openPage() {
  const user = userEvent.setup()
  const utils = renderApp('/cache')
  await screen.findByRole('heading', { level: 1, name: 'Cache Management' })
  return { user, ...utils }
}

async function clearViaDialog(user: ReturnType<typeof userEvent.setup>, opts: { reset?: boolean } = {}) {
  await user.click(screen.getByRole('button', { name: 'Clear cache…' }))
  const dialog = screen.getByRole('dialog', { name: 'Clear response cache' })
  await user.type(within(dialog).getByLabelText('Admin token'), ADMIN)
  if (opts.reset) await user.click(within(dialog).getByLabelText(/Also reset hit\/miss statistics/))
  await user.click(within(dialog).getByLabelText(/I understand/))
  await user.click(within(dialog).getByRole('button', { name: 'Clear cache' }))
}

function expectAdminTokenNowhere() {
  expect(document.documentElement.outerHTML).not.toContain(ADMIN)
  expect(window.localStorage.length).toBe(0)
  for (let i = 0; i < window.sessionStorage.length; i++) expect(window.sessionStorage.getItem(window.sessionStorage.key(i)!)).not.toContain(ADMIN)
  expect(credentialStore.get('observability')).not.toBe(ADMIN)
  expect(credentialStore.get('chat')).not.toBe(ADMIN)
}

afterEach(() => vi.useRealTimers())

describe('Cache Management: statistics', () => {
  it('renders the backend statistics, configuration and notes', async () => {
    serveStats()
    await openPage()
    expect(await kpi('Exact hits')).toHaveTextContent('7')
    expect(await kpi('Semantic hits')).toHaveTextContent('0')
    expect(await kpi('Misses')).toHaveTextContent('13')
    expect(await kpi('Hit rate')).toHaveTextContent('35.0%')
    expect(screen.getByText('20 lookups')).toBeInTheDocument()
    expect(await kpi('Total entries')).toHaveTextContent('12')
    expect(await kpi('Scopes')).toHaveTextContent('2')
    expect(await kpi('Approx. storage')).toHaveTextContent('20.0 KB')
    expect(await kpi('Stores')).toHaveTextContent('13')
    const cfg = screen.getByLabelText('Configuration')
    expect(cfg).toHaveTextContent('Response cacheEnabled')
    expect(cfg).toHaveTextContent('Redis reachableYes')
    expect(cfg).toHaveTextContent('Semantic (approximate) hitsDisabled')
    expect(cfg).toHaveTextContent('Embedding providerlocal-hashing-v1-256')
    expect(cfg).toHaveTextContent('Embeddings are semanticNo (lexical)')
    expect(cfg).toHaveTextContent('Similarity threshold0.92')
    expect(cfg).toHaveTextContent('Entry TTL3,600 s')
    expect(within(screen.getByTestId('backend-notes')).getAllByRole('listitem')).toHaveLength(3)
    expect(screen.getByText('Exact match only')).toBeInTheDocument()
  })

  it('semantic disabled + lexical embedder: says so, and claims no semantic understanding', async () => {
    serveStats()
    await openPage()
    await kpi('Semantic hits')
    expect(screen.getByText('Semantic hits are disabled')).toBeInTheDocument()
    expect(screen.getByTestId('cache-explainer')).toHaveTextContent('This backend uses the lexical embedder, so no semantic understanding is claimed here.')
    expect(screen.getByText(/similarity reflects shared words, not meaning/)).toBeInTheDocument()
  })

  it('semantic enabled with a meaning-aware (paid) embedder', async () => {
    serveStats(() => HttpResponse.json({ ...STATS, semantic_enabled: true, embedding_is_semantic: true, embedding_provider: 'litellm:text-embedding-3-small', hits_semantic: 4, hits: 11 }))
    await openPage()
    expect(await kpi('Semantic hits')).toHaveTextContent('4')
    expect(screen.getByText('Exact + semantic')).toBeInTheDocument()
    expect(screen.getByLabelText('Configuration')).toHaveTextContent('Yes (meaning-aware)')
    expect(screen.getByText(/each lookup that needs a vector is a paid call/)).toBeInTheDocument()
    expect(screen.getByTestId('cache-explainer')).not.toHaveTextContent('no semantic understanding is claimed')
  })

  it('flags the unsafe lexical-approximate override and highlights backend warnings', async () => {
    const warning = 'WARNING: approximate hits use the LEXICAL embedder (CACHE_SEMANTIC_ALLOW_LEXICAL=true); changed entities or word order can still be served a wrong cached answer.'
    serveStats(() => HttpResponse.json({ ...STATS, semantic_enabled: true, notes: [warning] }))
    await openPage()
    expect(await screen.findByText('Exact + lexical approximate (unsafe)')).toBeInTheDocument()
    expect(screen.getByText(warning).className).toContain('bg-amber-50')
  })

  it('Redis unreachable: counters are shown as unavailable, never as the 0 defaults', async () => {
    serveStats(() => HttpResponse.json(REDIS_DOWN))
    await openPage()
    for (const label of ['Exact hits', 'Semantic hits', 'Misses', 'Hit rate', 'Total entries', 'Scopes', 'Approx. storage', 'Stores']) {
      expect(await kpi(label)).toHaveTextContent('Unavailable')
    }
    expect(screen.getAllByText('Redis unreachable: not measured').length).toBeGreaterThan(0)
    expect(screen.getByText('Redis unreachable')).toBeInTheDocument()
    expect(screen.getByText('Redis unavailable (ConnectionError); counts unknown.')).toBeInTheDocument()
  })

  it('cache disabled: no fabricated zeros, embedder not configured', async () => {
    serveStats(() => HttpResponse.json(DISABLED))
    await openPage()
    expect(await kpi('Exact hits')).toHaveTextContent('Unavailable')
    expect(await kpi('Misses')).toHaveTextContent('Unavailable')
    expect(screen.getAllByText('Cache disabled: not measured').length).toBeGreaterThan(0)
    const cfg = screen.getByLabelText('Configuration')
    expect(cfg).toHaveTextContent('Response cacheDisabled')
    expect(cfg).toHaveTextContent('Redis reachableNot applicable')
    expect(cfg).toHaveTextContent('Embedding providerNot configured')
    expect(cfg).toHaveTextContent('Similarity thresholdUnavailable')
    expect(screen.getByText('Cache disabled (CACHE_ENABLED=false or REDIS_ENABLED=false).')).toBeInTheDocument()
  })

  it('empty cache: real zeros and no hit rate yet', async () => {
    serveStats(() => HttpResponse.json({ ...STATS, total_entries: 0, scopes: 0, hits: 0, hits_exact: 0, misses: 0, stores: 0, hit_rate: null, approx_storage_bytes: 0 }))
    await openPage()
    expect(await kpi('Total entries')).toHaveTextContent('0')
    expect(screen.getByText('Cache is empty')).toBeInTheDocument()
    expect(await kpi('Exact hits')).toHaveTextContent('0')
    expect(await kpi('Hit rate')).toHaveTextContent('—')
    expect(screen.getByText('No lookups yet')).toBeInTheDocument()
    expect(await kpi('Approx. storage')).toHaveTextContent('0 B')
  })
})

describe('Cache Management: requests (stats read has a side effect)', () => {
  it('explains the side effect and loads stats once; Refresh loads again; nothing polls', async () => {
    const calls = serveStats()
    const { user } = await openPage()
    await kpi('Exact hits')
    expect(screen.getByTestId('stats-side-effect')).toHaveTextContent(/not strictly read-only.*removes expired entries.*never polls/)
    expect(calls).toHaveLength(1)
    vi.useFakeTimers()
    vi.advanceTimersByTime(20 * 60_000)
    vi.useRealTimers()
    await new Promise((r) => setTimeout(r, 50))
    expect(calls).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(calls).toHaveLength(2))
  })

  it('keeps earlier statistics (and says so) when a refresh fails', async () => {
    serveStats((n) => (n === 1 ? HttpResponse.json(STATS) : errorResponse(503, 'service_unavailable', 'down')))
    const { user } = await openPage()
    await kpi('Exact hits')
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('Refresh failed · showing earlier data')).toBeInTheDocument()
    expect(await kpi('Exact hits')).toHaveTextContent('7')
  })

  it.each([
    [429, 'rate_limit_exceeded', 'Rate limit reached', { 'Retry-After': '15' }, 'Try again in 15s.'],
    [503, 'service_unavailable', 'Service unavailable', undefined, null],
  ])('stats HTTP %s shows %s', async (status, code, title, headers, hint) => {
    serveStats(() => errorResponse(status, code, `backend: ${code}`, { headers }))
    await openPage()
    expect(await screen.findByText(title)).toBeInTheDocument()
    if (hint) expect(screen.getByText(hint)).toBeInTheDocument()
    expect(screen.getByText('Not loaded')).toBeInTheDocument()
    expect(screen.queryByTestId('kpi-Exact hits')).toBeInTheDocument()
    expect(await kpi('Exact hits')).toHaveTextContent('—')
  })

  it('stats 401 asks for the observability token (not the admin token) and retries with it', async () => {
    const calls = serveStats((_n, auth) => (auth ? HttpResponse.json(STATS) : errorResponse(401, 'unauthorized', 'Missing or invalid observability token')))
    const { user } = await openPage()
    const prompt = await screen.findByRole('group', { name: 'Observability token required' })
    await user.type(within(prompt).getByPlaceholderText('Observability token'), 'obs-token-0123456789')
    await user.click(within(prompt).getByRole('button', { name: 'Use token' }))
    expect(await kpi('Exact hits')).toHaveTextContent('7')
    expect(calls.at(-1)).toBe('Bearer obs-token-0123456789')
  })

  it('network failure shows "Backend unreachable"', async () => {
    serveStats(() => HttpResponse.error())
    await openPage()
    expect((await screen.findAllByText('Backend unreachable')).length).toBeGreaterThan(0)
  })
})

describe('Cache Management: clearing', () => {
  it('clears with the admin token, shows the actual deletion result and does not re-read stats automatically', async () => {
    const statsCalls = serveStats()
    const clearCalls = serveClear(() => HttpResponse.json({ cleared: true, deleted_keys: 42, reset_stats: false }))
    const { user } = await openPage()
    await kpi('Exact hits')
    await clearViaDialog(user)
    const result = await screen.findByTestId('clear-result')
    expect(clearCalls).toEqual([{ auth: `Bearer ${ADMIN}`, resetStats: 'false' }])
    expect(within(result).getByTestId('deleted-keys')).toHaveTextContent('42')
    expect(result).toHaveTextContent('Statistics resetNo (counters kept)')
    expect(result).toHaveTextContent(/Redis keys .* not a count of cached answers/)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    // GET /cache/stats has a side effect: no automatic re-read after the clear
    await new Promise((r) => setTimeout(r, 100))
    expect(statsCalls).toHaveLength(1)
    expect(screen.getByTestId('stale-after-clear')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(statsCalls).toHaveLength(2))
    await waitFor(() => expect(screen.queryByTestId('stale-after-clear')).not.toBeInTheDocument())
    expectAdminTokenNowhere()
  })

  it('reset_stats=true is sent only when chosen and the backend result is shown', async () => {
    serveStats()
    const clearCalls = serveClear(() => HttpResponse.json({ cleared: true, deleted_keys: 0, reset_stats: true }))
    const { user } = await openPage()
    await kpi('Exact hits')
    await clearViaDialog(user, { reset: true })
    const result = await screen.findByTestId('clear-result')
    expect(clearCalls[0].resetStats).toBe('true')
    expect(result).toHaveTextContent('Statistics resetYes')
    expect(result).toHaveTextContent('Nothing was cached.')
  })

  it.each([
    [401, 'unauthorized', 'Admin token rejected'],
    [403, 'admin_disabled', 'Admin endpoints are disabled'],
    [503, 'cache_unavailable', 'Cache unavailable'],
  ])('clear HTTP %s %s: error shown, token discarded, no retry, no result', async (status, code, title) => {
    serveStats()
    const clearCalls = serveClear(() => errorResponse(status, code, `backend: ${code}`))
    const { user } = await openPage()
    await kpi('Exact hits')
    await clearViaDialog(user)
    expect(await screen.findByTestId('clear-error')).toHaveTextContent(title)
    await new Promise((r) => setTimeout(r, 100))
    expect(clearCalls).toHaveLength(1)
    expect(screen.queryByTestId('clear-result')).not.toBeInTheDocument()
    expect(within(screen.getByRole('dialog')).getByLabelText('Admin token')).toHaveValue('')
    expectAdminTokenNowhere()
    // stats session credential is untouched by the admin flow
    expect(credentialStore.has('observability')).toBe(false)
  })

  it('cancelling never sends a request and leaves no token behind', async () => {
    serveStats()
    const clearCalls = serveClear(() => HttpResponse.json({ cleared: true, deleted_keys: 1, reset_stats: false }))
    const { user } = await openPage()
    await kpi('Exact hits')
    await user.click(screen.getByRole('button', { name: 'Clear cache…' }))
    await user.type(screen.getByLabelText('Admin token'), ADMIN)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(clearCalls).toHaveLength(0)
    expectAdminTokenNowhere()
    // reopening starts empty
    await user.click(screen.getByRole('button', { name: 'Clear cache…' }))
    expect(screen.getByLabelText('Admin token')).toHaveValue('')
  })
})

describe('Cache Management: layout', () => {
  it('uses responsive grids (2→4 stat columns; explainer and admin side by side on xl)', async () => {
    serveStats()
    await openPage()
    const grid = (await kpi('Exact hits')).closest('.grid')!
    expect(grid.className).toContain('grid-cols-2')
    expect(grid.className).toContain('md:grid-cols-4')
    const split = screen.getByTestId('cache-explainer').closest('.grid')!
    expect(split.className).toContain('grid-cols-1')
    expect(split.className).toContain('xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]')
  })
})
