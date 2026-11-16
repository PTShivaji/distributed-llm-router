import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CredentialStore } from '../auth/credentials'
import { loadBudgetRefusals, loadBudgetSpend, loadBudgetStatus } from '../lib/budget'
import { loadSettings } from '../lib/settings'
import { refusalsFixture, settingsFixture, spendFixture, statusFixture, statusUnavailableFixture } from '../test/budgetFixtures'
import { errorResponse, server } from '../test/server'
import { createApiClient } from './client'
import { budgetQueryParams, createEndpoints } from './endpoints'
import { ApiError } from './errors'

const OBS_TOKEN = 'test-observability-token-SENTINEL-0001'
const CHAT_KEY = 'test-chat-key-SENTINEL-000000000001'

const ENDPOINTS = {
  settings: { path: '/api/v1/settings', body: settingsFixture, load: (api: Api) => loadSettings(api) },
  status: { path: '/api/v1/budget/status', body: statusFixture, load: (api: Api) => loadBudgetStatus(api) },
  spend: { path: '/api/v1/budget/spend', body: spendFixture, load: (api: Api) => loadBudgetSpend(api) },
  refusals: { path: '/api/v1/budget/refusals', body: refusalsFixture, load: (api: Api) => loadBudgetRefusals(api) },
} as const
type Api = ReturnType<typeof createEndpoints>
const NAMES = Object.keys(ENDPOINTS) as (keyof typeof ENDPOINTS)[]

let store: CredentialStore
let api: Api
let seen: Request[]

function respond(path: string, reply: () => Response) {
  server.use(http.get(path, ({ request }) => {
    seen.push(request.clone())
    return reply()
  }))
}

const consoleSpies: ReturnType<typeof vi.spyOn>[] = []

beforeEach(() => {
  store = new CredentialStore(null)
  api = createEndpoints(createApiClient({ baseURL: '', credentials: store }))
  seen = []
  for (const method of ['log', 'info', 'warn', 'error', 'debug'] as const) {
    consoleSpies.push(vi.spyOn(console, method).mockImplementation(() => {}))
  }
})

afterEach(() => {
  // Nothing in this module may ever log a credential (or anything else) to the console.
  for (const spy of consoleSpies) {
    const text = JSON.stringify(spy.mock.calls)
    expect(text).not.toContain(OBS_TOKEN)
    expect(text).not.toContain(CHAT_KEY)
    spy.mockRestore()
  }
  consoleSpies.length = 0
})

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (e) {
    expect(e).toBeInstanceOf(ApiError)
    return e as ApiError
  }
  throw new Error('expected the request to fail')
}

describe.each(NAMES)('%s endpoint', (name) => {
  const { path, body, load } = ENDPOINTS[name]

  it('returns the validated body and response metadata', async () => {
    respond(path, () => HttpResponse.json(body(), { headers: { 'X-Request-ID': 'srv-1' } }))
    const result = await load(api)
    expect(result.data).toEqual(body())
    expect(result.meta.status).toBe(200)
    expect(result.meta.requestId).toBe('srv-1')
  })

  it('sends the observability token (and only that credential) as a bearer token', async () => {
    store.set('observability', OBS_TOKEN)
    store.set('chat', CHAT_KEY)
    respond(path, () => HttpResponse.json(body()))
    await load(api)
    expect(seen[0].headers.get('authorization')).toBe(`Bearer ${OBS_TOKEN}`)
    expect(seen[0].url).not.toContain(OBS_TOKEN) // never in the query string
  })

  it('sends no Authorization header when no observability token is held', async () => {
    store.set('chat', CHAT_KEY)
    respond(path, () => HttpResponse.json(body()))
    await load(api)
    expect(seen[0].headers.get('authorization')).toBeNull()
  })

  it.each([
    [401, 'unauthorized', 'unauthorized'],
    [403, 'forbidden', 'forbidden'],
    [422, 'validation_error', 'validation'],
    [503, 'database_unavailable', 'unavailable'],
    [500, 'internal_error', 'server'],
  ])('maps HTTP %i to an ApiError (%s)', async (status, code, kind) => {
    store.set('observability', OBS_TOKEN)
    respond(path, () => errorResponse(status, code, 'backend message'))
    const err = await failure(load(api))
    expect(err).toMatchObject({ status, code, kind })
    expect(JSON.stringify(err.toJSON()) + err.message + String(err.stack)).not.toContain(OBS_TOKEN)
  })

  it('reports network failures without leaking the token', async () => {
    store.set('observability', OBS_TOKEN)
    respond(path, () => HttpResponse.error())
    const err = await failure(load(api))
    expect(err.kind).toBe('network')
    expect(JSON.stringify(err.toJSON())).not.toContain(OBS_TOKEN)
  })

  it('rejects malformed bodies instead of returning partial data', async () => {
    for (const bad of [null, [], 'text', { unexpected: true }, { ...body(), extra_field: OBS_TOKEN }]) {
      seen = []
      respond(path, () => HttpResponse.json(bad as never))
      const err = await failure(load(api))
      expect(err.code).toBe('malformed_response')
      expect(err.status).toBe(200)
      expect(err.message).toContain(path)
      expect(err.message).not.toContain(OBS_TOKEN) // values are never echoed into errors
    }
  })
})

describe('query parameters', () => {
  it('maps date ranges and pagination to the backend names and omits unset values', async () => {
    respond('/api/v1/budget/spend', () => HttpResponse.json(spendFixture()))
    respond('/api/v1/budget/refusals', () => HttpResponse.json(refusalsFixture()))
    await loadBudgetSpend(api, { startDate: '2026-11-01', endDate: '2026-11-03' })
    await loadBudgetRefusals(api, { startDate: '2026-11-01', page: 2, pageSize: 25 })
    await loadBudgetSpend(api)
    const params = seen.map((r) => Object.fromEntries(new URL(r.url).searchParams))
    expect(params[0]).toEqual({ start_date: '2026-11-01', end_date: '2026-11-03' })
    expect(params[1]).toEqual({ start_date: '2026-11-01', page: '2', page_size: '25' })
    expect(params[2]).toEqual({})
    expect(budgetQueryParams({})).toEqual({})
  })

  it.each([
    ['spend', () => loadBudgetSpend(api, { startDate: '2026/11/01' })],
    ['spend reversed', () => loadBudgetSpend(api, { startDate: '2026-11-03', endDate: '2026-11-01' })],
    ['refusals page 0', () => loadBudgetRefusals(api, { page: 0 })],
    ['refusals page size 101', () => loadBudgetRefusals(api, { pageSize: 101 })],
    ['refusals fractional page', () => loadBudgetRefusals(api, { page: 1.5 })],
  ])('rejects invalid parameters before sending (%s)', async (_name, call) => {
    const err = await failure(call())
    expect(err).toMatchObject({ code: 'invalid_parameters', kind: 'validation', status: null })
    expect(seen).toHaveLength(0) // onUnhandledRequest='error' would also fail a stray request
  })

  it('passes backend range validation errors through (400 invalid_time_range)', async () => {
    respond('/api/v1/budget/spend', () => errorResponse(400, 'invalid_time_range', 'end_date must not be in the future (UTC).'))
    const err = await failure(loadBudgetSpend(api, { endDate: '2099-01-01' }))
    expect(err).toMatchObject({ status: 400, code: 'invalid_time_range', kind: 'bad_request' })
  })
})

describe('unavailable values are preserved', () => {
  it('keeps null spend and the reason from the backend (never 0)', async () => {
    respond('/api/v1/budget/status', () => HttpResponse.json(statusUnavailableFixture()))
    const { data } = await loadBudgetStatus(api)
    expect(data.today.counted_usd).toBeNull()
    expect(data.today.remaining_usd).toBeNull()
    expect(data.today.unavailable_reason).toBe('the budget store (Redis) is unreachable')
    expect(data.notes.length).toBeGreaterThan(0)
  })
})

describe('credential storage is unchanged', () => {
  it('never writes the observability token to localStorage', async () => {
    const real = new CredentialStore(window.sessionStorage)
    const client = createEndpoints(createApiClient({ baseURL: '', credentials: real }))
    real.set('observability', OBS_TOKEN)
    respond('/api/v1/settings', () => HttpResponse.json(settingsFixture()))
    await loadSettings(client)
    expect(JSON.stringify({ ...window.localStorage })).not.toContain(OBS_TOKEN)
    expect(window.sessionStorage.length).toBe(0) // memory only unless the user opts in
  })
})
