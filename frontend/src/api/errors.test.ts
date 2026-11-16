import { AxiosError, AxiosHeaders, CanceledError } from 'axios'
import { describe, expect, it } from 'vitest'

import { ApiError, errorHint, kindFor, normalizeError, parseRetryAfter } from './errors'

describe('parseRetryAfter', () => {
  const now = Date.parse('2026-11-09T10:00:00Z')
  it.each([
    ['42', 42],
    [' 5 ', 5],
    ['Mon, 09 Nov 2026 10:00:30 GMT', 30],
    ['Mon, 09 Nov 2026 09:59:00 GMT', 0],
    [12.2, 13],
    ['soon', null],
    ['', null],
    [undefined, null],
  ])('%s -> %s', (input, expected) => {
    expect(parseRetryAfter(input, now)).toBe(expected)
  })
})

describe('kindFor', () => {
  it('distinguishes budget refusals from rate limits and provider rejections from validation', () => {
    expect(kindFor(429, 'budget_exceeded')).toBe('budget_exceeded')
    expect(kindFor(429, 'rate_limit_exceeded')).toBe('rate_limited')
    expect(kindFor(422, 'request_rejected_by_provider')).toBe('rejected')
    expect(kindFor(422, 'validation_error')).toBe('validation')
    expect(kindFor(502, 'http_502')).toBe('server')
    expect(kindFor(405, 'method_not_allowed')).toBe('bad_request')
  })
})

describe('normalizeError', () => {
  it('passes ApiError through unchanged', () => {
    const e = new ApiError({ message: 'm', status: 400, code: 'c', kind: 'bad_request' })
    expect(normalizeError(e)).toBe(e)
  })

  it('maps timeouts, cancellations and unknown values', () => {
    expect(normalizeError(new AxiosError('t', 'ECONNABORTED')).kind).toBe('timeout')
    expect(normalizeError(new CanceledError()).kind).toBe('cancelled')
    expect(normalizeError(new Error('boom'))).toMatchObject({ kind: 'unknown', status: null })
    expect(normalizeError('weird')).toMatchObject({ kind: 'unknown' })
  })

  const raw = (status: number, data: unknown) => normalizeError(new AxiosError('x', 'ERR_BAD_RESPONSE', undefined, undefined,
    { status, statusText: '', data, headers: new AxiosHeaders(), config: { headers: new AxiosHeaders() } } as never))

  it('uses a generic message when the body is not the standard envelope', () => {
    expect(raw(500, '<html>oops</html>')).toMatchObject({ status: 500, code: 'http_500', kind: 'server', message: 'The server reported an error.' })
  })

  it('treats a 502/504 without the backend envelope as "backend unreachable" (proxy could not connect)', () => {
    for (const status of [502, 504]) {
      expect(raw(status, '')).toMatchObject({ status, code: 'backend_unreachable', kind: 'network' })
    }
    expect(raw(502, '')).toHaveProperty('message', expect.stringMatching(/Cannot reach the backend API/))
  })

  it('drops the Authorization header carried by the Axios error config', () => {
    const config = { headers: new AxiosHeaders({ Authorization: 'Bearer secret-value-123456789' }) }
    const response = { status: 401, statusText: '', data: { error: { code: 'unauthorized', message: 'no' } }, headers: new AxiosHeaders(), config }
    const e = normalizeError(new AxiosError('x', 'ERR_BAD_REQUEST', config as never, undefined, response as never))
    expect(JSON.stringify(e)).not.toContain('secret-value')
    expect(e).not.toHaveProperty('config')
  })
})

describe('errorHint', () => {
  it('gives actionable hints for auth, admin_disabled, 429 and budget store outages', () => {
    const make = (kind: ApiError['kind'], code: string, retryAfterSeconds: number | null = null) =>
      new ApiError({ message: 'm', status: 0, code, kind, retryAfterSeconds })
    expect(errorHint(make('unauthorized', 'unauthorized'))).toMatch(/Settings/)
    expect(errorHint(make('forbidden', 'admin_disabled'))).toMatch(/ADMIN_API_TOKEN/)
    expect(errorHint(make('rate_limited', 'rate_limit_exceeded', 9))).toBe('Try again in 9s.')
    expect(errorHint(make('unavailable', 'budget_unavailable'))).toMatch(/budget store/)
  })
})
