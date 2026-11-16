import { useEffect, useEffectEvent } from 'react'

/**
 * Calls `onTick` every `intervalMs` while enabled. Skips ticks while the tab is hidden and
 * until `pausedUntil` (epoch ms, e.g. from a 429 Retry-After). Read endpoints only.
 */
export function useAutoRefresh(onTick: () => void, { intervalMs, enabled, pausedUntil = 0 }: {
  intervalMs: number
  enabled: boolean
  pausedUntil?: number
}): void {
  const tick = useEffectEvent(() => {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
    if (Date.now() < pausedUntil) return
    onTick()
  })

  useEffect(() => {
    if (!enabled) return
    const id = window.setInterval(() => tick(), intervalMs)
    return () => window.clearInterval(id)
  }, [enabled, intervalMs])
}
