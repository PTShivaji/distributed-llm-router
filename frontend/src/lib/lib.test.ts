import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { formatInteger, formatLatency, formatPercent, formatUsd } from './format'
import { hourlyWindows, mapWithConcurrency } from './hourly'

describe('format', () => {
  it('renders missing values as an em dash, never as zero', () => {
    for (const f of [formatInteger, formatPercent, formatLatency, formatUsd]) {
      expect(f(null)).toBe('—')
      expect(f(undefined)).toBe('—')
    }
  })

  it('formats real values', () => {
    expect(formatInteger(1234567)).toBe('1,234,567')
    expect(formatPercent(0.256)).toBe('25.6%')
    expect(formatLatency(4.567)).toBe('4.57 ms')
    expect(formatLatency(2500)).toBe('2.50 s')
    expect(formatUsd(0)).toBe('$0.00')
    expect(formatUsd(0.0000206)).toBe('$0.000021')
    expect(formatUsd(12.5)).toBe('$12.50')
  })
})

describe('hourlyWindows', () => {
  it('returns 24 contiguous, hour-aligned UTC windows ending with the current hour', () => {
    const now = new Date('2026-11-09T10:37:12Z')
    const w = hourlyWindows(now)
    expect(w).toHaveLength(24)
    expect(w[23].start.toISOString()).toBe('2026-11-09T10:00:00.000Z')
    expect(w[23].end.toISOString()).toBe('2026-11-09T11:00:00.000Z')
    expect(w[0].start.toISOString()).toBe('2026-11-08T11:00:00.000Z')
    for (let i = 1; i < w.length; i++) expect(w[i].start.getTime()).toBe(w[i - 1].end.getTime())
  })
})

describe('mapWithConcurrency', () => {
  it('never exceeds the limit and preserves order', async () => {
    let active = 0
    let peak = 0
    const out = await mapWithConcurrency([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 4, async (n) => {
      active += 1
      peak = Math.max(peak, active)
      await new Promise((r) => setTimeout(r, 5))
      active -= 1
      return n * 2
    })
    expect(out).toEqual([2, 4, 6, 8, 10, 12, 14, 16, 18, 20])
    expect(peak).toBeLessThanOrEqual(4)
  })

  it('stops starting new work after abort', async () => {
    const controller = new AbortController()
    let started = 0
    const run = mapWithConcurrency([1, 2, 3, 4, 5, 6], 2, async () => {
      started += 1
      controller.abort()
      return 0
    }, controller.signal)
    await expect(run).rejects.toThrow('Aborted')
    expect(started).toBe(1) // the first call aborted; no further work was started
  })
})

describe('useAutoRefresh', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('ticks on the interval, skips while hidden and while paused by Retry-After', () => {
    vi.useFakeTimers()
    const onTick = vi.fn()
    let hidden = false
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => (hidden ? 'hidden' : 'visible'))
    const { rerender, unmount } = renderHook(({ pausedUntil }) => useAutoRefresh(onTick, { intervalMs: 1000, enabled: true, pausedUntil }),
      { initialProps: { pausedUntil: 0 } })
    vi.advanceTimersByTime(1000)
    expect(onTick).toHaveBeenCalledTimes(1)
    hidden = true
    vi.advanceTimersByTime(3000)
    expect(onTick).toHaveBeenCalledTimes(1)
    hidden = false
    rerender({ pausedUntil: Date.now() + 2500 })
    vi.advanceTimersByTime(2000)
    expect(onTick).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(1000)
    expect(onTick).toHaveBeenCalledTimes(2)
    unmount()
    vi.advanceTimersByTime(5000)
    expect(onTick).toHaveBeenCalledTimes(2)
  })

  it('does nothing when disabled', () => {
    vi.useFakeTimers()
    const onTick = vi.fn()
    renderHook(() => useAutoRefresh(onTick, { intervalMs: 1000, enabled: false }))
    vi.advanceTimersByTime(10_000)
    expect(onTick).not.toHaveBeenCalled()
  })
})
