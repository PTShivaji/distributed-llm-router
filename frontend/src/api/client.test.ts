import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { CredentialStore } from '../auth/credentials'
import { errorResponse, server } from '../test/server'
import { createApiClient, newRequestId } from './client'
import { createEndpoints } from './endpoints'
import { ApiError } from './errors'

const CHAT_KEY = 'test-chat-key-0000000000000001'
const OBS_TOKEN = 'test-observability-token-00000001'
const ADMIN_TOKEN = 'test-admin-token-000000000000001'

let store: CredentialStore
let api: ReturnType<typeof createEndpoints>
let seen: Request[]

function record(path: string, method: 'get' | 'post' | 'delete', respond: () => Response) {
  server.use(http[method](path, ({ request }) => {
    seen.push(request.clone())
    return respond()
  }))
}

beforeEach(() => {
  store = new CredentialStore(null)
  api = createEndpoints(createApiClient({ baseURL: '', credentials: store }))
  seen = []
})

const CHAT_BODY = { query: 'hello' }

describe('request ids and response metadata', () => {
  it('generates a backend-valid X-Request-ID for every request', async () => {
    record('/api/v1/models', 'get', () => HttpResponse.json({ models: [] }))
    await api.models()
    const id = seen[0].headers.get('x-request-id')
    expect(id).toMatch(/^[A-Za-z0-9._-]{1,64}$/)
    expect(newRequestId()).not.toBe(newRequestId())
  })

  it('returns X-Request-ID, X-Trace-ID and rate-limit headers in meta', async () => {
    record('/api/v1/chat', 'post', () => HttpResponse.json({ answer: 'x' }, { headers: {
      'X-Request-ID': 'srv-1', 'X-Trace-ID': 'a'.repeat(32), 'X-RateLimit-Limit': '60',
      'X-RateLimit-Remaining': '59', 'X-RateLimit-Reset': '30', 'X-RateLimit-Window': 'minute',
    } }))
    const { meta } = await api.chat(CHAT_BODY)
    expect(meta).toEqual({ status: 200, requestId: 'srv-1', traceId: 'a'.repeat(32),
      rateLimit: { limit: 60, remaining: 59, resetSeconds: 30, window: 'minute' } })
  })
})

describe('credential injection', () => {
  it('sends no Authorization header when no credential is configured', async () => {
    record('/api/v1/chat', 'post', () => HttpResponse.json({}))
    await api.chat(CHAT_BODY)
    expect(seen[0].headers.get('authorization')).toBeNull()
  })

  it('sends the chat key only to /chat and the observability token only to observability endpoints', async () => {
    store.set('chat', CHAT_KEY)
    store.set('observability', OBS_TOKEN)
    record('/api/v1/chat', 'post', () => HttpResponse.json({}))
    record('/api/v1/analytics/summary', 'get', () => HttpResponse.json({}))
    record('/api/v1/cache/stats', 'get', () => HttpResponse.json({}))
    record('/api/v1/health/dependencies', 'get', () => HttpResponse.json({}))
    record('/metrics', 'get', () => new HttpResponse('# metrics', { headers: { 'Content-Type': 'text/plain' } }))
    record('/api/v1/models', 'get', () => HttpResponse.json({}))
    record('/health', 'get', () => HttpResponse.json({}))

    await api.chat(CHAT_BODY)
    await api.analyticsSummary()
    await api.cacheStats()
    await api.dependencies()
    await api.metricsText()
    await api.models()
    await api.health()

    const auth = seen.map((r) => [new URL(r.url).pathname, r.headers.get('authorization')])
    expect(auth).toEqual([
      ['/api/v1/chat', `Bearer ${CHAT_KEY}`],
      ['/api/v1/analytics/summary', `Bearer ${OBS_TOKEN}`],
      ['/api/v1/cache/stats', `Bearer ${OBS_TOKEN}`],
      ['/api/v1/health/dependencies', `Bearer ${OBS_TOKEN}`],
      ['/metrics', `Bearer ${OBS_TOKEN}`],
      ['/api/v1/models', null],
      ['/health', null],
    ])
  })

  it('reads the credential at send time (updates apply to the next request)', async () => {
    record('/api/v1/chat', 'post', () => HttpResponse.json({}))
    await api.chat(CHAT_BODY)
    store.set('chat', CHAT_KEY)
    await api.chat(CHAT_BODY)
    store.clear('chat')
    await api.chat(CHAT_BODY)
    expect(seen.map((r) => r.headers.get('authorization'))).toEqual([null, `Bearer ${CHAT_KEY}`, null])
  })

  it('uses the admin token for the cache-clear request only and never stores it', async () => {
    record('/api/v1/cache', 'delete', () => HttpResponse.json({ cleared: true, deleted_keys: 3, reset_stats: true }))
    record('/api/v1/cache/stats', 'get', () => HttpResponse.json({}))
    const { data } = await api.clearCache(ADMIN_TOKEN, true)
    await api.cacheStats()
    expect(data.deleted_keys).toBe(3)
    expect(seen[0].headers.get('authorization')).toBe(`Bearer ${ADMIN_TOKEN}`)
    expect(new URL(seen[0].url).searchParams.get('reset_stats')).toBe('true')
    expect(seen[1].headers.get('authorization')).toBeNull() // not remembered anywhere
    expect(store.has('chat') || store.has('observability')).toBe(false)
  })
})

describe('test isolation', () => {
  it('fails any request without an MSW handler (nothing can reach a real backend)', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    // MSW reports the unhandled request on console.warn: expected here, so captured and asserted.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const unhandled: string[] = []
    const listener = ({ request }: { request: Request }) => {
      unhandled.push(`${request.method} ${new URL(request.url).pathname}`)
    }
    server.events.on('request:unhandled', listener)
    try {
      await expect(api.chat(CHAT_BODY)).rejects.toMatchObject({ kind: 'network' })
    } finally {
      server.events.removeListener('request:unhandled', listener)
    }
    expect(unhandled).toEqual(['POST /api/v1/chat']) // intercepted by MSW, then failed: never sent
    const mswOutput = [...warn.mock.calls, ...vi.mocked(console.error).mock.calls].flat().join(' ')
    expect(mswOutput).toMatch(/\[MSW\].*without a matching request handler/)
  })
})

describe('no automatic retries', () => {
  it.each([
    [503, 'all_models_failed'],
    [503, 'budget_unavailable'],
    [429, 'budget_exceeded'],
    [500, 'internal_error'],
  ])('sends a paid chat request exactly once on %i %s', async (status, code) => {
    record('/api/v1/chat', 'post', () => errorResponse(status, code, 'nope'))
    await expect(api.chat(CHAT_BODY)).rejects.toBeInstanceOf(ApiError)
    expect(seen).toHaveLength(1)
  })

  it('does not retry on network errors either', async () => {
    record('/api/v1/chat', 'post', () => HttpResponse.error())
    await expect(api.chat(CHAT_BODY)).rejects.toMatchObject({ kind: 'network', status: null })
    expect(seen).toHaveLength(1)
  })
})

describe('error normalisation over HTTP', () => {
  it('maps 429 with Retry-After to rate_limited with retryAfterSeconds', async () => {
    record('/api/v1/chat', 'post', () => errorResponse(429, 'rate_limit_exceeded', 'Rate limit exceeded: 2 requests per minute.', {
      details: { limit: 2, window: 'minute', retry_after_seconds: 41 }, headers: { 'Retry-After': '42', 'X-Request-ID': 'srv-429' },
    }))
    const error = await api.chat(CHAT_BODY).catch((e: unknown) => e)
    expect(error).toMatchObject({ status: 429, code: 'rate_limit_exceeded', kind: 'rate_limited', retryAfterSeconds: 42,
      requestId: 'srv-429', message: 'Rate limit exceeded: 2 requests per minute.' })
  })

  it('falls back to details.retry_after_seconds when the header is missing', async () => {
    record('/api/v1/chat', 'post', () => errorResponse(429, 'rate_limit_exceeded', 'slow', { details: { retry_after_seconds: 7 } }))
    await expect(api.chat(CHAT_BODY)).rejects.toMatchObject({ retryAfterSeconds: 7 })
  })

  it.each([
    [401, 'unauthorized', 'unauthorized'],
    [403, 'admin_disabled', 'forbidden'],
    [404, 'not_found', 'not_found'],
    [400, 'model_disabled', 'bad_request'],
    [413, 'request_too_large', 'payload_too_large'],
    [422, 'validation_error', 'validation'],
    [422, 'request_rejected_by_provider', 'rejected'],
    [429, 'budget_exceeded', 'budget_exceeded'],
    [503, 'budget_unavailable', 'unavailable'],
  ])('maps %i %s to kind %s', async (status, code, kind) => {
    record('/api/v1/chat', 'post', () => errorResponse(status, code, `msg ${code}`, { details: { x: 1 } }))
    await expect(api.chat(CHAT_BODY)).rejects.toMatchObject({ status, code, kind, message: `msg ${code}`, details: { x: 1 } })
  })

  it('parses the JSON error envelope of text endpoints (/metrics 401)', async () => {
    record('/metrics', 'get', () => errorResponse(401, 'unauthorized', 'Missing or invalid observability token.'))
    await expect(api.metricsText()).rejects.toMatchObject({ kind: 'unauthorized', code: 'unauthorized',
      message: 'Missing or invalid observability token.' })
  })

  it('returns /metrics as raw text', async () => {
    record('/metrics', 'get', () => new HttpResponse('llm_router_build_info{version="0.1.0"} 1.0\n', {
      headers: { 'Content-Type': 'text/plain; version=1.0.0' } }))
    const { data } = await api.metricsText()
    expect(data).toBe('llm_router_build_info{version="0.1.0"} 1.0\n')
  })

  it('never carries request headers or credentials inside the error', async () => {
    store.set('chat', CHAT_KEY)
    record('/api/v1/chat', 'post', () => errorResponse(401, 'unauthorized', 'Invalid chat API key.'))
    const error = await api.chat(CHAT_BODY).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    const dump = `${JSON.stringify(error)} ${String(error)} ${Object.keys(error as object).join(',')}`
    expect(dump).not.toContain(CHAT_KEY)
    expect(dump).not.toMatch(/authorization|config|headers/i)
  })
})
