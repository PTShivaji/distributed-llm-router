import { describe, expect, it } from 'vitest'

import {
  CAPTURED, clone, event, refusalsFixture, spendFixture, statusFixture, statusNotConfiguredFixture,
  statusUnavailableFixture,
} from '../test/budgetFixtures'
import {
  budgetRefusalsCheck, budgetSpendCheck, budgetStatusCheck, checkRange, costBasis, countedTodayText,
  isBudgetRefusalsResponse, isBudgetSpendResponse, isBudgetStatusResponse, limitText, NOT_CONFIGURED_TEXT,
  processValueText, remainingTodayText, UNAVAILABLE_TEXT,
} from './budget'
import { explain } from './validate'

const SECRET_LOOKING = 'sk-SENTINEL-must-never-be-echoed-0001'

describe('budget status validation', () => {
  it('accepts the real backend body and the documented variants', () => {
    expect(explain(budgetStatusCheck, CAPTURED.status)).toBeNull()
    for (const body of [statusFixture(), statusUnavailableFixture(), statusNotConfiguredFixture()]) {
      expect(isBudgetStatusResponse(body)).toBe(true)
    }
  })

  it('keeps unknown values null and explains why', () => {
    const s = statusUnavailableFixture()
    expect(s.today.counted_usd).toBeNull()
    expect(countedTodayText(s)).toBe(UNAVAILABLE_TEXT)
    expect(remainingTodayText(s)).toBe(UNAVAILABLE_TEXT)
    expect(s.today.unavailable_reason).toMatch(/unreachable/)
    const none = statusNotConfiguredFixture()
    expect(remainingTodayText(none)).toBe(NOT_CONFIGURED_TEXT)
    expect(limitText(none.limits.daily_usd)).toBe(NOT_CONFIGURED_TEXT)
    for (const text of [countedTodayText(s), remainingTodayText(s), remainingTodayText(none)]) expect(text).not.toMatch(/\$0/)
  })

  it('shows a real zero only when the backend reported it', () => {
    const s = statusFixture()
    expect(countedTodayText(s)).toBe('$0.00')
    expect(remainingTodayText(s)).toBe('$5.00')
    expect(limitText(1)).toBe('$1.00')
  })

  it('never reports monthly enforcement', () => {
    const s = statusFixture()
    expect(s.monthly.enforcement_supported).toBe(false)
    const lying = clone(s) as unknown as { monthly: { enforcement_supported: boolean; limit_usd: number | null } }
    lying.monthly.enforcement_supported = true
    expect(isBudgetStatusResponse(lying)).toBe(false)
    lying.monthly.enforcement_supported = false
    lying.monthly.limit_usd = 100
    expect(isBudgetStatusResponse(lying)).toBe(false)
  })

  it.each([
    ['missing section', (b: Record<string, unknown>) => { delete b.today }, 'body.today: missing'],
    ['unexpected field', (b: Record<string, unknown>) => { b.debug_dump = SECRET_LOOKING }, 'unexpected field'],
    ['zero where unknown', (b: Record<string, unknown>) => {
      Object.assign(b.today as object, { available: false, counted_usd: 0, unavailable_reason: 'x' })
    }, 'available: inconsistent'],
    ['unavailable without reason', (b: Record<string, unknown>) => {
      Object.assign(b.today as object, { available: false, counted_usd: null, remaining_usd: null, unavailable_reason: null })
    }, 'unavailable_reason'],
    ['remaining without a limit', (b: Record<string, unknown>) => {
      (b.limits as Record<string, unknown>).daily_usd = null
    }, 'remaining_usd'],
    ['negative amount', (b: Record<string, unknown>) => { (b.today as Record<string, unknown>).counted_usd = -1 }, 'number >= 0'],
    ['per-client counters exposed', (b: Record<string, unknown>) => {
      (b.today as Record<string, unknown>).per_client_counters_exposed = true
    }, 'per_client_counters_exposed'],
    ['wrong store', (b: Record<string, unknown>) => { (b.enforcement as Record<string, unknown>).store = SECRET_LOOKING }, 'store'],
    ['process values while metrics are off', (b: Record<string, unknown>) => {
      (b.embedding as Record<string, unknown>).process_counters_available = false
    }, 'counters are unavailable'],
  ])('rejects malformed bodies: %s', (_name, mutate, expected) => {
    const body = clone(statusFixture()) as unknown as Record<string, unknown>
    mutate(body)
    const problem = explain(budgetStatusCheck, body)
    expect(problem).toContain(expected)
    expect(problem).not.toContain(SECRET_LOOKING) // values are never echoed
  })
})

describe('budget spend validation', () => {
  it('accepts the real backend body (zero-filled days) and a mixed history', () => {
    expect(isBudgetSpendResponse(CAPTURED.spend)).toBe(true)
    expect(CAPTURED.spend.days).toHaveLength(3)
    expect(isBudgetSpendResponse(spendFixture())).toBe(true)
  })

  it.each([
    ['a missing day', (b: ReturnType<typeof spendFixture>) => { b.days.splice(1, 1) }, 'one entry per day'],
    ['days out of order', (b: ReturnType<typeof spendFixture>) => { b.days.reverse() }, 'not consecutive'],
    ['inconsistent counts', (b: ReturnType<typeof spendFixture>) => { b.totals.mock_requests = 99 }, 'mock_requests + real_requests'],
    ['successful above total', (b: ReturnType<typeof spendFixture>) => { b.days[0].successful_requests = 9 }, 'exceeds requests'],
    ['embedding spend claimed', (b: ReturnType<typeof spendFixture>) => {
      (b as unknown as Record<string, unknown>).embedding_spend_included = true
    }, 'embedding_spend_included'],
    ['bad date', (b: ReturnType<typeof spendFixture>) => { b.start_date = '2026-02-30' }, 'date YYYY-MM-DD'],
    ['null cost', (b: ReturnType<typeof spendFixture>) => {
      (b.by_provider[0] as unknown as Record<string, unknown>).real_estimated_cost_usd = null
    }, 'by_provider[0].real_estimated_cost_usd'],
  ])('rejects %s', (_name, mutate, expected) => {
    const body = spendFixture()
    mutate(body)
    expect(explain(budgetSpendCheck, body)).toContain(expected)
  })

  it('labels mock cost as simulated and real cost as an estimate, never as billed', () => {
    const mockOnly = { requests: 2, mock_requests: 2, real_requests: 0 }
    expect(costBasis(mockOnly)).toEqual({ kind: 'simulated', label: expect.stringContaining('Simulated') })
    expect(costBasis(mockOnly).label).toMatch(/placeholder prices; no money spent/)
    expect(costBasis({ requests: 1, mock_requests: 0, real_requests: 1 }).label).toMatch(/Estimated .*not a provider invoice/)
    expect(costBasis(spendFixture().totals).kind).toBe('mixed')
    expect(costBasis(CAPTURED.spend.totals)).toEqual({ kind: 'none', label: 'No persisted requests' })
    for (const f of [mockOnly, spendFixture().totals]) expect(costBasis(f).label).not.toMatch(/\bbill(ed)?\b|invoiced|charged/i)
  })
})

describe('budget refusals validation', () => {
  it('accepts the real (empty) backend body and a paginated page', () => {
    expect(isBudgetRefusalsResponse(CAPTURED.refusals)).toBe(true)
    expect(CAPTURED.refusals.events).toEqual([])
    expect(isBudgetRefusalsResponse(refusalsFixture())).toBe(true)
  })

  it.each([
    ['unknown reason', (b: ReturnType<typeof refusalsFixture>) => {
      (b.events[0] as unknown as Record<string, unknown>).reason = 'weird'
    }, 'events[0].reason'],
    ['client identifier field', (b: ReturnType<typeof refusalsFixture>) => {
      (b.events[0] as unknown as Record<string, unknown>).client_hash = SECRET_LOOKING
    }, 'unexpected field'],
    ['total mismatch', (b: ReturnType<typeof refusalsFixture>) => { b.pagination.total = 1 }, 'pagination.total'],
    ['more events than page size', (b: ReturnType<typeof refusalsFixture>) => { b.events.push(event(), event()) }, 'page_size'],
    ['page 0', (b: ReturnType<typeof refusalsFixture>) => { b.pagination.page = 0 }, 'page: must be >= 1'],
    ['page size above max', (b: ReturnType<typeof refusalsFixture>) => { b.pagination.page_size = 101 }, 'page_size: out of range'],
    ['embedding refusals claimed persisted', (b: ReturnType<typeof refusalsFixture>) => {
      (b as unknown as Record<string, unknown>).embedding_refusals_persisted = true
    }, 'embedding_refusals_persisted'],
    ['process counters while unavailable', (b: ReturnType<typeof refusalsFixture>) => { b.process_counters_available = false },
      'process counters present'],
  ])('rejects %s', (_name, mutate, expected) => {
    const body = refusalsFixture()
    mutate(body)
    const problem = explain(budgetRefusalsCheck, body)
    expect(problem).toContain(expected)
    expect(problem).not.toContain(SECRET_LOOKING)
  })

  it('accepts null process counters when metrics are disabled', () => {
    const body = refusalsFixture()
    body.process_counters_available = false
    body.process_chat_refusals = null
    body.process_embedding_refusals = null
    expect(isBudgetRefusalsResponse(body)).toBe(true)
    expect(processValueText(null)).toBe(UNAVAILABLE_TEXT)
    expect(processValueText(0)).toBe('$0.00')
  })
})

describe('client-side parameter checks', () => {
  it('accepts valid ranges and rejects invalid ones without contacting the backend', () => {
    expect(() => checkRange({})).not.toThrow()
    expect(() => checkRange({ startDate: '2026-11-01', endDate: '2026-11-01' })).not.toThrow()
    expect(() => checkRange({ startDate: '2026-08-02', endDate: '2026-11-01' })).not.toThrow() // 92 days
    for (const bad of [{ startDate: '2026-11-1' }, { endDate: 'yesterday' }, { startDate: '2026-02-30' },
      { startDate: '2026-11-02', endDate: '2026-11-01' }, { startDate: '2026-08-01', endDate: '2026-11-01' }]) {
      expect(() => checkRange(bad)).toThrowError(expect.objectContaining({ code: 'invalid_parameters', kind: 'validation' }))
    }
  })
})
