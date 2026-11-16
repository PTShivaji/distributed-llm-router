import { describe, expect, it } from 'vitest'

import { spendFixture, statusFixture, statusNotConfiguredFixture, statusUnavailableFixture } from '../test/budgetFixtures'
import {
  activeDays, addDays, counterLabelText, customDayRangeError, dailyCostRows, enforcementState, formatUtc, pageCount, pageWindow,
  readBudgetRange, reasonText, resolveBudgetRange, uniqueNotes, utcToday, utilization, writeBudgetRange,
} from './budgetDashboard'

const NOW = Date.parse('2026-11-11T23:30:00Z')

describe('budget date range', () => {
  it('uses UTC days', () => {
    expect(utcToday(NOW)).toBe('2026-11-11')
    expect(addDays('2026-11-01', -1)).toBe('2026-10-31')
    expect(addDays('2026-02-28', 1)).toBe('2026-03-01')
  })

  it('resolves presets to explicit inclusive ranges ending today (UTC)', () => {
    expect(resolveBudgetRange({ key: '7d' }, NOW)).toEqual({ startDate: '2026-11-05', endDate: '2026-11-11' })
    expect(resolveBudgetRange({ key: '30d' }, NOW)).toEqual({ startDate: '2026-10-13', endDate: '2026-11-11' })
    expect(resolveBudgetRange({ key: '90d' }, NOW)).toEqual({ startDate: '2026-08-14', endDate: '2026-11-11' })
    expect(resolveBudgetRange({ key: 'custom', start: '2026-01-01', end: '2026-01-31' }, NOW))
      .toEqual({ startDate: '2026-01-01', endDate: '2026-01-31' })
  })

  it('validates custom ranges like the backend', () => {
    expect(customDayRangeError('2026-11-01', '2026-11-11', '2026-11-11')).toBeNull()
    expect(customDayRangeError('', '2026-11-11', '2026-11-11')).toMatch(/both/)
    expect(customDayRangeError('2026-11-xx', '2026-11-11', '2026-11-11')).toMatch(/YYYY-MM-DD/)
    expect(customDayRangeError('2026-11-05', '2026-11-04', '2026-11-11')).toMatch(/after/)
    expect(customDayRangeError('2026-11-05', '2026-11-12', '2026-11-11')).toMatch(/future/)
    expect(customDayRangeError('2026-08-12', '2026-11-11', '2026-11-11')).toBeNull() // exactly 92 days, inclusive
    expect(customDayRangeError('2026-08-11', '2026-11-11', '2026-11-11')).toMatch(/92 days/) // 93
  })

  it('reads and writes the URL, falling back to the default for anything invalid', () => {
    expect(readBudgetRange(new URLSearchParams(''))).toEqual({ key: '30d' })
    expect(readBudgetRange(new URLSearchParams('range=7d'))).toEqual({ key: '7d' })
    expect(readBudgetRange(new URLSearchParams('range=custom&start=2026-11-01&end=2026-11-03')))
      .toEqual({ key: 'custom', start: '2026-11-01', end: '2026-11-03' })
    for (const bad of ['range=1y', 'range=custom', 'range=custom&start=2026-11-05&end=2026-11-01', 'range=custom&start=2026-01-01&end=2026-12-31',
      'range=custom&start=x&end=y']) {
      expect(readBudgetRange(new URLSearchParams(bad))).toEqual({ key: '30d' })
    }
    const keep = new URLSearchParams('other=1&start=a')
    expect(writeBudgetRange(keep, { key: '30d' }).toString()).toBe('other=1')
    expect(writeBudgetRange(keep, { key: 'custom', start: '2026-11-01', end: '2026-11-02' }).toString())
      .toBe('other=1&range=custom&start=2026-11-01&end=2026-11-02')
    expect(keep.toString()).toBe('other=1&start=a') // input untouched
  })
})

describe('budget overview helpers', () => {
  it('computes utilization only from reported values', () => {
    const s = statusFixture()
    expect(utilization(s)).toBe(0)
    s.today.counted_usd = 2.5
    expect(utilization(s)).toBe(0.5)
    expect(utilization(statusUnavailableFixture())).toBeNull()
    expect(utilization(statusNotConfiguredFixture())).toBeNull()
    s.limits.daily_usd = 0
    expect(utilization(s)).toBeNull()
  })

  it('describes enforcement from the backend flags only', () => {
    expect(enforcementState(statusNotConfiguredFixture()).label).toBe('Not configured')
    expect(enforcementState(statusFixture()).label).toBe('Configured · inactive')
    const active = statusFixture()
    active.enforcement.currently_applicable = true
    expect(enforcementState(active)).toMatchObject({ label: 'Active', tone: 'success' })
    const down = statusUnavailableFixture()
    down.enforcement.currently_applicable = true
    expect(enforcementState(down)).toMatchObject({ label: 'Active · counters unavailable', tone: 'warning',
      detail: 'the budget store (Redis) is unreachable' })
  })
})

describe('spend and refusal helpers', () => {
  it('keeps simulated and estimated costs separate per day', () => {
    const rows = dailyCostRows(spendFixture().days)
    expect(rows.map((r) => [r.date, r.label, r.real, r.mock])).toEqual([
      ['2026-11-01', 'Nov 1', 0, 0.005], ['2026-11-02', 'Nov 2', 0.05, 0], ['2026-11-03', 'Nov 3', 0, 0]])
    expect(activeDays(spendFixture().days).map((d) => d.date)).toEqual(['2026-11-01', '2026-11-02'])
  })

  it('pagination text', () => {
    expect(pageCount({ page: 1, page_size: 20, total: 0, has_more: false })).toBe(1)
    expect(pageCount({ page: 1, page_size: 20, total: 41, has_more: true })).toBe(3)
    expect(pageWindow({ page: 3, page_size: 20, total: 41, has_more: false }, 1)).toBe('41–41 of 41')
    expect(pageWindow({ page: 9, page_size: 20, total: 41, has_more: false }, 0)).toBeNull()
  })

  it('labels', () => {
    expect(reasonText('global_daily_budget')).toBe('Global daily budget')
    expect(reasonText('something_new')).toBe('something_new')
    expect(formatUtc('2026-11-03T10:00:00Z')).toBe('2026-11-03 10:00 UTC')
    expect(formatUtc('2026-11-03T15:30:00+05:30')).toBe('2026-11-03 10:00 UTC')
    expect(counterLabelText({ labels: {}, value: 1 })).toBe('(no labels)')
    expect(counterLabelText({ labels: { a: '1', b: '2' }, value: 1 })).toBe('a=1, b=2')
    expect(uniqueNotes(['x', 'y'], null, ['y', 'z'], undefined)).toEqual(['x', 'y', 'z'])
  })
})
