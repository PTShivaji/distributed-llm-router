/**
 * Centralised API client (Axios).
 *
 * - Every request gets an X-Request-ID (backend rule: [A-Za-z0-9._-]{1,64}); the response's
 *   X-Request-ID / X-Trace-ID are returned in `meta` for correlation with backend logs.
 * - Credentials are injected per request by `credential` ('chat' | 'observability'), read
 *   from the runtime store at send time. An Authorization header is never inherited.
 * - The admin token is accepted only as a per-call `adminToken` (cache clear) and never stored.
 * - Errors are normalised to ApiError (safe fields only, no request config / headers).
 * - NO automatic retries anywhere. In particular a paid chat request is sent exactly once:
 *   the backend already retries/falls back under its own budget, and a client retry could
 *   double spend. On 429 the caller gets `retryAfterSeconds` and decides.
 */

import axios, { AxiosHeaders, type AxiosInstance, type AxiosRequestConfig } from 'axios'

import { credentialStore, type CredentialKind, type CredentialStore } from '../auth/credentials'
import { DEFAULT_TIMEOUT_MS } from '../config'
import { normalizeError } from './errors'

export const REQUEST_ID_HEADER = 'X-Request-ID'
export const TRACE_ID_HEADER = 'X-Trace-ID'
const VALID_REQUEST_ID = /^[A-Za-z0-9._-]{1,64}$/

declare module 'axios' {
  interface AxiosRequestConfig {
    /** Which runtime credential to send as a bearer token (default: none). */
    credential?: CredentialKind
    /** One-off admin bearer token (cache clear only). Never stored by the client. */
    adminToken?: string
  }
}

export interface RateLimitInfo {
  limit: number | null
  remaining: number | null
  resetSeconds: number | null
  window: string | null
}

export interface ResponseMeta {
  status: number
  requestId: string | null
  traceId: string | null
  rateLimit: RateLimitInfo | null
}

export interface ApiResult<T> {
  data: T
  meta: ResponseMeta
}

export function newRequestId(): string {
  const random = globalThis.crypto?.randomUUID?.().replace(/-/g, '')
  return `ui-${random ?? `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`}`
}

function toNumber(value: unknown): number | null {
  if (typeof value !== 'string' || value.trim() === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function readMeta(status: number, headers: AxiosHeaders): ResponseMeta {
  const get = (name: string): string | null => {
    const v = headers.get(name)
    return typeof v === 'string' && v ? v : null
  }
  const limit = get('x-ratelimit-limit')
  return {
    status,
    requestId: get('x-request-id'),
    traceId: get('x-trace-id'),
    rateLimit: limit === null ? null : {
      limit: toNumber(limit),
      remaining: toNumber(get('x-ratelimit-remaining')),
      resetSeconds: toNumber(get('x-ratelimit-reset')),
      window: get('x-ratelimit-window'),
    },
  }
}

type NetworkErrorListener = () => void
const networkErrorListeners = new Set<NetworkErrorListener>()

/**
 * Called whenever a request fails without reaching the backend (kind 'network'). Used by the shared
 * backend-health state to re-check /health promptly instead of showing a stale "Connected" badge.
 */
export function onNetworkError(listener: NetworkErrorListener): () => void {
  networkErrorListeners.add(listener)
  return () => networkErrorListeners.delete(listener)
}

export interface ApiClientOptions {
  baseURL: string
  credentials?: CredentialStore
  timeoutMs?: number
}

export interface ApiClient {
  readonly http: AxiosInstance
  request<T>(config: AxiosRequestConfig): Promise<ApiResult<T>>
}

export function createApiClient({ baseURL, credentials = credentialStore, timeoutMs = DEFAULT_TIMEOUT_MS }: ApiClientOptions): ApiClient {
  const http = axios.create({
    baseURL,
    timeout: timeoutMs,
    headers: { Accept: 'application/json' },
    withCredentials: false, // bearer tokens only; no cookies
  })

  http.interceptors.request.use((config) => {
    const headers = AxiosHeaders.from(config.headers)
    const supplied = headers.get(REQUEST_ID_HEADER)
    if (typeof supplied !== 'string' || !VALID_REQUEST_ID.test(supplied)) {
      headers.set(REQUEST_ID_HEADER, newRequestId())
    }
    headers.delete('Authorization') // only the explicit mechanisms below may set it
    const token = config.adminToken ?? (config.credential ? credentials.get(config.credential) : null)
    if (token) headers.set('Authorization', `Bearer ${token}`)
    config.headers = headers
    return config
  })

  http.interceptors.response.use(undefined, (error: unknown) => {
    const normalized = normalizeError(error)
    if (normalized.kind === 'network') {
      for (const listener of networkErrorListeners) {
        try {
          listener()
        } catch {
          /* a listener must never change how the request fails */
        }
      }
    }
    return Promise.reject(normalized)
  })

  return {
    http,
    async request<T>(config: AxiosRequestConfig): Promise<ApiResult<T>> {
      const response = await http.request<T>(config)
      return { data: response.data, meta: readMeta(response.status, AxiosHeaders.from(response.headers as AxiosHeaders)) }
    },
  }
}
