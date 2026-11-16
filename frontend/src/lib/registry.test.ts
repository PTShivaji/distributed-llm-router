import { describe, expect, it } from 'vitest'

import { modelFixture } from '../test/server'
import {
  filterModels, formatPricePerMillion, formatSeconds, hasActiveFilters, NO_FILTERS, resolveFallbacks, routerHealth, sortModels,
  strategyDescription, summarizeRegistry, tiersOf,
} from './registry'

const models = [
  modelFixture('b-simple', { priority: 20 }),
  modelFixture('a-simple', { priority: 20 }),
  modelFixture('top', { tier: 'COMPLEX', provider: 'openai', priority: 1 }),
  modelFixture('off', { enabled: false, tier: 'MEDIUM', pricing_is_placeholder: false, fallbacks: [] }),
  modelFixture('mid', { tier: 'MEDIUM', provider: 'groq', provider_model: 'llama-x', fallbacks: ['off', 'gone', 'top'] }),
]

describe('registry data shaping', () => {
  it('summarizes counts from the data only', () => {
    expect(summarizeRegistry(models)).toEqual({ total: 5, enabled: 4, disabled: 1, providers: ['groq', 'mock', 'openai'], placeholderPricing: 4 })
    expect(summarizeRegistry([])).toEqual({ total: 0, enabled: 0, disabled: 0, providers: [], placeholderPricing: 0 })
  })

  it('lists tiers present, in complexity order', () => {
    expect(tiersOf(models)).toEqual(['SIMPLE', 'MEDIUM', 'COMPLEX'])
    expect(tiersOf([models[2]])).toEqual(['COMPLEX'])
  })

  it('filters by query, provider, status and tier', () => {
    expect(filterModels(models, NO_FILTERS)).toHaveLength(5)
    expect(filterModels(models, { ...NO_FILTERS, query: ' LLAMA ' }).map((m) => m.id)).toEqual(['mid'])
    expect(filterModels(models, { ...NO_FILTERS, provider: 'openai' }).map((m) => m.id)).toEqual(['top'])
    expect(filterModels(models, { ...NO_FILTERS, status: 'disabled' }).map((m) => m.id)).toEqual(['off'])
    expect(filterModels(models, { ...NO_FILTERS, status: 'enabled', tier: 'MEDIUM' }).map((m) => m.id)).toEqual(['mid'])
    expect(hasActiveFilters(NO_FILTERS)).toBe(false)
    expect(hasActiveFilters({ ...NO_FILTERS, query: '  ' })).toBe(false)
    expect(hasActiveFilters({ ...NO_FILTERS, tier: 'SIMPLE' })).toBe(true)
  })

  it('sorts by tier, then priority (lower first), then id', () => {
    expect(sortModels(models).map((m) => m.id)).toEqual(['a-simple', 'b-simple', 'mid', 'off', 'top'])
  })

  it('resolves fallbacks against the registry without dropping or adding ids', () => {
    expect(resolveFallbacks(models[4], models)).toEqual([
      { id: 'off', status: 'disabled' }, { id: 'gone', status: 'unregistered' }, { id: 'top', status: 'enabled' },
    ])
  })

  it('never reports a disabled model as routable, whatever its tracked state', () => {
    const disabled = modelFixture('x', { enabled: false })
    expect(routerHealth(disabled)).toMatchObject({ label: 'Not routed', tone: 'neutral' })
    expect(routerHealth(modelFixture('y', { health: { ...disabled.health, state: 'unhealthy' } }))).toMatchObject({ tone: 'danger' })
    expect(routerHealth(modelFixture('z', { health: { ...disabled.health, state: 'degraded' } }))).toMatchObject({ tone: 'warning' })
    expect(routerHealth(modelFixture('w', { health: { ...disabled.health, state: 'mystery' } }))).toMatchObject({ label: 'mystery', tone: 'neutral' })
  })

  it('formats prices exactly and describes strategies', () => {
    expect(formatPricePerMillion(0)).toBe('$0.00')
    expect(formatPricePerMillion(0.1)).toBe('$0.10')
    expect(formatPricePerMillion(0.075)).toBe('$0.075')
    expect(formatPricePerMillion(15)).toBe('$15.00')
    expect(formatPricePerMillion(0.00002)).toBe('$0.000020')
    expect(formatSeconds(30)).toBe('30 s')
    expect(formatSeconds(2.5)).toBe('2.5 s')
    expect(strategyDescription('lowest_cost')).toMatch(/blended price/)
    expect(strategyDescription('priority')).toMatch(/priority number/)
  })
})
