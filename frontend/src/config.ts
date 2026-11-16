/**
 * Build-time configuration. ONLY the API base URL is read from the environment:
 * every VITE_* value is embedded in the browser bundle, so no secret may ever live here.
 * (ESLint enforces this with a no-restricted-syntax rule.)
 */

/** Empty string = same origin (the Vite dev proxy forwards /api, /health and /metrics). */
export function resolveApiBaseUrl(raw: string | undefined): string {
  const value = (raw ?? '').trim().replace(/\/+$/, '')
  if (value === '') return ''
  if (!/^https?:\/\/[^\s/]+/i.test(value)) {
    throw new Error('VITE_API_BASE_URL must be empty or an http(s) URL')
  }
  return value
}

export const API_BASE_URL = resolveApiBaseUrl(import.meta.env.VITE_API_BASE_URL)

export const APP_TITLE = 'Distributed AI Router'

/** Chat requests can wait for retries and fallbacks server-side; this is a client-side ceiling. */
export const CHAT_TIMEOUT_MS = 120_000
export const DEFAULT_TIMEOUT_MS = 15_000
