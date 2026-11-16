/**
 * Cache Management helpers over GET /api/v1/cache/stats.
 *
 * Backend facts (backend/app/api/cache.py, backend/app/cache/semantic.py):
 *  - When the cache is disabled, or Redis is unreachable during the stats read, the counters
 *    (hits, misses, stores) are returned as 0 DEFAULTS, not measurements. They are therefore
 *    shown as unavailable here, never as real zeros.
 *  - hit_rate = (exact + semantic hits) / (hits + misses), null without lookups.
 */

import type { ApiError } from '../api/errors'
import type { CacheStatsResponse } from '../api/types'

export type CacheMode =
  | 'disabled' // CACHE_ENABLED=false or REDIS_ENABLED=false
  | 'unavailable' // enabled, but Redis could not be read
  | 'exact_only' // exact-match caching only (semantic off or gated)
  | 'semantic' // approximate hits with a meaning-aware embedder
  | 'semantic_lexical' // approximate hits with the lexical embedder (unsafe override)

export function cacheMode(s: CacheStatsResponse): CacheMode {
  if (!s.enabled) return 'disabled'
  if (!s.available) return 'unavailable'
  if (!s.semantic_enabled) return 'exact_only'
  return s.embedding_is_semantic ? 'semantic' : 'semantic_lexical'
}

/** Counters are real measurements only when the cache is enabled AND Redis was read successfully. */
export function countersMeasured(s: CacheStatsResponse): boolean {
  return s.enabled && s.available
}

/** A counter value, or null when it is a default rather than a measurement. */
export function measured(s: CacheStatsResponse, value: number): number | null {
  return countersMeasured(s) ? value : null
}

export interface LookupPart {
  key: 'exact' | 'semantic' | 'miss'
  label: string
  value: number
}

/**
 * How the measured lookups split (exact hits, semantic hits, misses), in display order.
 * null when the counters are defaults rather than measurements (cache off / Redis unread).
 */
export function lookupSplit(s: CacheStatsResponse): { parts: LookupPart[]; total: number } | null {
  if (!countersMeasured(s)) return null
  const parts: LookupPart[] = [
    { key: 'exact', label: 'Exact hits', value: s.hits_exact },
    { key: 'semantic', label: 'Semantic hits', value: s.hits_semantic },
    { key: 'miss', label: 'Misses', value: s.misses },
  ]
  return { parts, total: parts.reduce((sum, p) => sum + p.value, 0) }
}

export function lookups(s: CacheStatsResponse): number | null {
  return countersMeasured(s) ? s.hits + s.misses : null
}

export interface EmbedderView {
  label: string
  kind: 'lexical' | 'semantic' | 'none'
  description: string
}

export function embedderView(s: CacheStatsResponse): EmbedderView {
  if (!s.embedding_provider) return { label: 'Not configured', kind: 'none', description: 'No embedder is reported (the cache is disabled).' }
  if (!s.embedding_is_semantic) {
    return { label: s.embedding_provider, kind: 'lexical',
      description: 'Local hashing embedder: free and offline, but similarity reflects shared words, not meaning.' }
  }
  return { label: s.embedding_provider, kind: 'semantic',
    description: 'Meaning-aware embeddings from a provider API: each lookup that needs a vector is a paid call, subject to the backend embedding budget.' }
}

/** User-facing explanation of a failed DELETE /api/v1/cache (the admin token is never involved in retries). */
export function clearErrorView(error: ApiError): { title: string; explanation: string } {
  if (error.code === 'admin_disabled') {
    return { title: 'Admin endpoints are disabled', explanation: 'The backend has no ADMIN_API_TOKEN configured, so the cache cannot be cleared from the UI.' }
  }
  if (error.kind === 'unauthorized') {
    return { title: 'Admin token rejected', explanation: 'The token was missing or did not match ADMIN_API_TOKEN. It has been discarded; enter it again to retry.' }
  }
  if (error.code === 'cache_unavailable') {
    return { title: 'Cache unavailable', explanation: 'The cache is disabled or Redis is unreachable, so nothing was cleared.' }
  }
  if (error.kind === 'rate_limited') {
    return { title: 'Rate limit reached', explanation: error.retryAfterSeconds !== null ? `Try again in ${error.retryAfterSeconds}s.` : 'Try again shortly.' }
  }
  if (error.kind === 'network') return { title: 'Backend unreachable', explanation: 'The request may not have reached the backend. Check Refresh before trying again.' }
  if (error.kind === 'timeout') return { title: 'Request timed out', explanation: 'The cache may or may not have been cleared. Refresh the statistics before trying again.' }
  if (error.kind === 'unavailable') return { title: 'Service unavailable', explanation: 'Nothing is retried automatically.' }
  return { title: 'Cache was not cleared', explanation: 'Nothing is retried automatically.' }
}
