import { useCallback, useEffect, useEffectEvent, useMemo, useState } from 'react'

import type { ResponseMeta } from '../api/client'
import { normalizeError, type ApiError } from '../api/errors'

export interface QueryState<T> {
  data: T | null
  meta: ResponseMeta | null
  error: ApiError | null
  /** True until the CURRENT request settles (previous data stays visible while reloading). */
  loading: boolean
  /** Epoch ms when the last request settled (success or error); null before the first. */
  updatedAt: number | null
  reload: () => void
}

interface Settled<T> {
  request: object
  data: T | null
  meta: ResponseMeta | null
  error: ApiError | null
  at: number
}

export interface QueryResult<T> {
  data: T
  meta?: ResponseMeta | null
}

/**
 * Loads data on mount, when `key` changes and on reload(), with abort on unmount/key change
 * (stale requests are cancelled). For READ endpoints only: paid actions such as chat are
 * triggered explicitly by the user, never here. Errors keep the previous data visible.
 */
export function useApiQuery<T>(fetcher: (signal: AbortSignal) => Promise<QueryResult<T>>, key: unknown = null): QueryState<T> {
  const [tick, setTick] = useState(0)
  // A fresh identity per (key, reload) request; loading = "the current request has not settled yet".
  // eslint-disable-next-line react-hooks/exhaustive-deps -- key and tick are the request identity
  const request = useMemo(() => ({}), [key, tick])
  const [settled, setSettled] = useState<Settled<T> | null>(null)
  const load = useEffectEvent((signal: AbortSignal) => fetcher(signal))

  useEffect(() => {
    const controller = new AbortController()
    load(controller.signal).then(
      (result) => {
        if (!controller.signal.aborted) {
          setSettled({ request, data: result.data, meta: result.meta ?? null, error: null, at: Date.now() })
        }
      },
      (err: unknown) => {
        const error = normalizeError(err)
        if (!controller.signal.aborted && error.kind !== 'cancelled') {
          setSettled((prev) => ({ request, data: prev?.data ?? null, meta: prev?.meta ?? null, error, at: Date.now() }))
        }
      },
    )
    return () => controller.abort()
  }, [request])

  const reload = useCallback(() => setTick((t) => t + 1), [])
  const current = settled?.request === request
  return {
    data: settled?.data ?? null,
    meta: settled?.meta ?? null,
    error: current ? (settled?.error ?? null) : null,
    loading: !current,
    updatedAt: settled?.at ?? null,
    reload,
  }
}
