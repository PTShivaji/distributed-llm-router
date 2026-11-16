import { describe, expect, it } from 'vitest'

import { ApiError, type ApiErrorKind } from '../api/errors'
import { modelFixture } from '../test/server'
import {
  budgetReasons, buildChatRequest, cacheLabel, describeChatError, errorAttempts, isCacheHit, MAX_QUERY_LENGTH, queryProblem,
  selectableModels, usageSourceLabel, validationMessages, validOverride,
} from './chat'
import { splitFences } from './fences'

const err = (status: number | null, code: string, kind: ApiErrorKind) => new ApiError({ message: 'm', status, code, kind })

describe('chat request building and validation', () => {
  it('builds the exact request body (model only when overriding)', () => {
    expect(buildChatRequest({ query: 'q', model: null, useCache: true })).toEqual({ query: 'q', use_cache: true })
    expect(buildChatRequest({ query: 'q', model: 'm1', useCache: false })).toEqual({ query: 'q', use_cache: false, model: 'm1' })
  })

  it('mirrors the backend query limits', () => {
    expect(queryProblem('')).toBe('empty')
    expect(queryProblem(' \n ')).toBe('empty')
    expect(queryProblem('x'.repeat(MAX_QUERY_LENGTH))).toBeNull()
    expect(queryProblem('x'.repeat(MAX_QUERY_LENGTH + 1))).toBe('too_long')
  })

  it('only allows registered, enabled models as overrides', () => {
    const models = [modelFixture('on'), modelFixture('off', { enabled: false })]
    expect(selectableModels(models).map((m) => m.id)).toEqual(['on'])
    expect(validOverride('on', models)).toBe('on')
    expect(validOverride('off', models)).toBeNull()
    expect(validOverride('ghost', models)).toBeNull()
    expect(validOverride('on', null)).toBeNull()
    expect(validOverride(null, models)).toBeNull()
  })
})

describe('chat error descriptions', () => {
  it('distinguishes rate limits from budget refusals and model errors from validation', () => {
    expect(describeChatError(err(429, 'rate_limit_exceeded', 'rate_limited')).title).toBe('Rate limit reached')
    expect(describeChatError(err(429, 'budget_exceeded', 'budget_exceeded')).title).toBe('Spend limit reached')
    expect(describeChatError(err(400, 'unknown_model', 'bad_request'))).toMatchObject({ title: 'Model not registered', modelProblem: true })
    expect(describeChatError(err(400, 'model_disabled', 'bad_request'))).toMatchObject({ title: 'Model disabled', modelProblem: true })
    expect(describeChatError(err(422, 'validation_error', 'validation'))).toMatchObject({ title: 'Request failed validation', modelProblem: false })
    expect(describeChatError(err(422, 'request_rejected_by_provider', 'rejected')).title).toBe('Rejected by the provider')
    expect(describeChatError(err(401, 'unauthorized', 'unauthorized'))).toMatchObject({ needsCredential: true })
    expect(describeChatError(err(413, 'request_too_large', 'payload_too_large')).title).toBe('Request too large')
    expect(describeChatError(err(503, 'budget_unavailable', 'unavailable')).title).toBe('Budget store unavailable')
    expect(describeChatError(err(null, 'network_error', 'network')).title).toBe('Backend unreachable')
    expect(describeChatError(err(null, 'timeout', 'timeout')).explanation).toMatch(/may still have processed/)
    expect(describeChatError(err(503, 'something_new', 'unavailable')).title).toBe('Service unavailable')
  })

  it('parses error details defensively', () => {
    expect(errorAttempts({ attempts: [{ model: 'a', outcome: 'error', error_kind: 'timeout' }, { model: 'b', reason: 'daily' }, 'junk', {}] }))
      .toEqual([{ model: 'a', outcome: 'error', reason: 'timeout' }, { model: 'b', outcome: null, reason: 'daily' }])
    expect(errorAttempts(null)).toEqual([])
    expect(budgetReasons({ reasons: ['daily', 3, ''] })).toEqual(['daily'])
    expect(validationMessages([{ loc: ['body', 'query'], msg: 'too long' }, { msg: 'general' }, 'x'])).toEqual(['query: too long', 'general'])
    expect(validationMessages({ not: 'a list' })).toEqual([])
  })

  it('labels cache and usage sources, passing unknown values through unchanged', () => {
    expect(cacheLabel('hit_exact')).toBe('Exact cache hit')
    expect(cacheLabel('future_status')).toBe('future_status')
    expect(isCacheHit('hit_semantic')).toBe(true)
    expect(isCacheHit('miss')).toBe(false)
    expect(usageSourceLabel('estimated')).toBe('Estimated by the router')
    expect(usageSourceLabel('other')).toBe('other')
  })
})

describe('splitFences', () => {
  it('separates prose and fenced code, including an unterminated fence', () => {
    expect(splitFences('a\n```js\nx = 1\n```\nb')).toEqual([
      { kind: 'text', content: 'a\n' }, { kind: 'code', content: 'x = 1', lang: 'js' }, { kind: 'text', content: '\nb' },
    ])
    expect(splitFences('```\nopen')).toEqual([{ kind: 'code', content: 'open', lang: undefined }])
    expect(splitFences('plain')).toEqual([{ kind: 'text', content: 'plain' }])
  })
})
