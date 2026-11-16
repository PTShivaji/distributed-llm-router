import { useCallback, useSyncExternalStore } from 'react'

/**
 * Live result of a CSS media query. `fallback` is used where matchMedia does not exist
 * (server rendering, some test environments).
 */
export function useMediaQuery(query: string, fallback: boolean): boolean {
  const subscribe = useCallback((onChange: () => void) => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
    const list = window.matchMedia(query)
    list.addEventListener('change', onChange)
    return () => list.removeEventListener('change', onChange)
  }, [query])
  const read = () =>
    typeof window === 'undefined' || typeof window.matchMedia !== 'function' ? fallback : window.matchMedia(query).matches
  return useSyncExternalStore(subscribe, read, () => fallback)
}

/** Tailwind's `lg` breakpoint (64rem): the sidebar is permanently visible from here up. */
export const DESKTOP_QUERY = '(min-width: 64rem)'
