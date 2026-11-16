/**
 * Shared GET /health state: the top bar and the Overview header read ONE request instead of
 * each fetching their own. refresh() re-checks (used by the Overview refresh control).
 *
 * While at least one component is subscribed, the state is kept current:
 *   - re-checked every HEALTH_RECHECK_MS while the tab is visible (hidden tabs send nothing),
 *   - re-checked as soon as the tab becomes visible again,
 *   - re-checked shortly after any API request fails with a network error (debounced), so the
 *     badge never keeps saying "Connected" while the backend is down.
 * /health is public and touches neither Redis nor PostgreSQL.
 */

import { useEffect, useSyncExternalStore } from 'react'

import { onNetworkError } from '../api/client'
import { api } from '../api/endpoints'
import { normalizeError, type ApiError } from '../api/errors'
import type { HealthResponse } from '../api/types'

export const HEALTH_RECHECK_MS = 30_000
/** At most one network-error-triggered re-check per this interval. */
export const NETWORK_RECHECK_MIN_GAP_MS = 5_000

export interface BackendHealth {
  status: 'idle' | 'checking' | 'connected' | 'unreachable'
  data: HealthResponse | null
  error: ApiError | null
}

const INITIAL: BackendHealth = { status: 'idle', data: null, error: null }
let state: BackendHealth = INITIAL
let inflight: Promise<void> | null = null
let lastCheckStarted = 0
const listeners = new Set<() => void>()

function set(next: BackendHealth) {
  state = next
  for (const l of listeners) l()
}

export function refreshBackendHealth(): Promise<void> {
  if (inflight) return inflight
  lastCheckStarted = Date.now()
  // Only the very first check shows "checking"; later re-checks keep the last known status (no flicker).
  set({ ...state, status: state.status === 'idle' ? 'checking' : state.status })
  inflight = api.health().then(
    ({ data }) => set({ status: 'connected', data, error: null }),
    (err: unknown) => set({ status: 'unreachable', data: null, error: normalizeError(err) }),
  ).finally(() => {
    inflight = null
  })
  return inflight
}

function visible(): boolean {
  return typeof document === 'undefined' || document.visibilityState !== 'hidden'
}

// ---- keep-current machinery: active only while something is subscribed ----
let timer: ReturnType<typeof setInterval> | null = null
let unsubscribeNetwork: (() => void) | null = null

function onVisibilityChange() {
  if (visible()) void refreshBackendHealth()
}

function onNetworkFailure() {
  // Debounced; the /health request itself failing does not loop (inflight is reused).
  if (Date.now() - lastCheckStarted >= NETWORK_RECHECK_MIN_GAP_MS) void refreshBackendHealth()
}

function start() {
  timer = setInterval(() => {
    if (visible()) void refreshBackendHealth()
  }, HEALTH_RECHECK_MS)
  unsubscribeNetwork = onNetworkError(onNetworkFailure)
  if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibilityChange)
}

function stop() {
  if (timer !== null) clearInterval(timer)
  timer = null
  unsubscribeNetwork?.()
  unsubscribeNetwork = null
  if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibilityChange)
}

/** Test helper: forget the cached result and stop background re-checks. */
export function resetBackendHealth(): void {
  inflight = null
  lastCheckStarted = 0
  state = INITIAL
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  if (listeners.size === 1) start()
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0) stop()
  }
}

export function useBackendHealth(): BackendHealth {
  const snapshot = useSyncExternalStore(subscribe, () => state, () => state)
  useEffect(() => {
    if (state.status === 'idle') void refreshBackendHealth()
  }, [])
  return snapshot
}
