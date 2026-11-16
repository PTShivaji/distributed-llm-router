/**
 * Standardised API errors.
 *
 * Every backend error uses one envelope (backend/app/core/errors.py):
 *   {"error": {"code": "<machine_code>", "message": "<text>", "details": ...}}
 * (This envelope is not described in the OpenAPI schema, so it is typed here.)
 *
 * normalizeError() turns any Axios/network failure into an ApiError that carries ONLY
 * safe fields. The original Axios error is never kept: its `config` holds request
 * headers, including the Authorization bearer token.
 */

import { isAxiosError, isCancel } from 'axios'

import { API_BASE_URL } from '../config'

/**
 * Where API calls go: the configured VITE_API_BASE_URL, or this page's origin when it is empty
 * (same-origin deployment, or the Vite dev proxy). Never a hard-coded development address.
 */
export function apiTarget(base: string = API_BASE_URL): string {
  if (base) return base
  const origin = typeof window !== 'undefined' ? window.location?.origin : undefined
  return origin && origin !== 'null' ? origin : 'this site'
}

/** True when the API target is this machine (local development), judged from the URL only. */
export function isLocalTarget(target: string = apiTarget()): boolean {
  try {
    return ['localhost', '127.0.0.1', '[::1]', '::1'].includes(new URL(target).hostname)
  } catch {
    return false
  }
}

export function networkMessage(base: string = API_BASE_URL): string {
  return `Cannot reach the backend API at ${apiTarget(base)}.`
}

export interface ErrorEnvelope {
  error: { code: string; message: string; details?: unknown }
}

export type ApiErrorKind =
  | 'unauthorized' // 401: missing/invalid chat key, observability or admin token
  | 'forbidden' // 403: e.g. admin_disabled
  | 'not_found' // 404
  | 'bad_request' // 400 / 405: unknown_model, model_disabled, invalid_time_range
  | 'payload_too_large' // 413
  | 'validation' // 422 validation_error
  | 'rejected' // 422 request_rejected_by_provider (content policy)
  | 'rate_limited' // 429 rate_limit_exceeded
  | 'budget_exceeded' // 429 budget_exceeded
  | 'unavailable' // 503: all_models_failed, budget_unavailable, *_unavailable
  | 'server' // other 5xx
  | 'network' // no response (backend down, proxy error, CORS)
  | 'timeout'
  | 'cancelled'
  | 'unknown'

export class ApiError extends Error {
  override readonly name = 'ApiError'
  readonly status: number | null
  readonly code: string
  readonly kind: ApiErrorKind
  readonly details: unknown
  /**
   * Seconds to wait before retrying (429 rate limit, or 503 such as "rate limiter busy"), from the
   * Retry-After header or details.retry_after_seconds. null when the backend gave no hint.
   */
  readonly retryAfterSeconds: number | null
  /** X-Request-ID of the failed request (for correlating with backend logs). */
  readonly requestId: string | null

  constructor(init: {
    message: string
    status: number | null
    code: string
    kind: ApiErrorKind
    details?: unknown
    retryAfterSeconds?: number | null
    requestId?: string | null
  }) {
    super(init.message)
    this.status = init.status
    this.code = init.code
    this.kind = init.kind
    this.details = init.details ?? null
    this.retryAfterSeconds = init.retryAfterSeconds ?? null
    this.requestId = init.requestId ?? null
  }

  toJSON() {
    return {
      name: this.name, message: this.message, status: this.status, code: this.code, kind: this.kind,
      details: this.details, retryAfterSeconds: this.retryAfterSeconds, requestId: this.requestId,
    }
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError
}

function isEnvelope(data: unknown): data is ErrorEnvelope {
  if (typeof data !== 'object' || data === null || !('error' in data)) return false
  const inner = (data as { error: unknown }).error
  return typeof inner === 'object' && inner !== null && typeof (inner as { code?: unknown }).code === 'string'
}

/** Retry-After is either delta-seconds or an HTTP date. Returns whole seconds (>= 0) or null. */
export function parseRetryAfter(value: unknown, now: number = Date.now()): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.max(0, Math.ceil(value))
  if (typeof value !== 'string' || value.trim() === '') return null
  const trimmed = value.trim()
  if (/^\d+$/.test(trimmed)) return Number(trimmed)
  const date = Date.parse(trimmed)
  return Number.isNaN(date) ? null : Math.max(0, Math.ceil((date - now) / 1000))
}

export function kindFor(status: number, code: string): ApiErrorKind {
  if (status === 401) return 'unauthorized'
  if (status === 403) return 'forbidden'
  if (status === 404) return 'not_found'
  if (status === 413) return 'payload_too_large'
  if (status === 422) return code === 'request_rejected_by_provider' ? 'rejected' : 'validation'
  if (status === 429) return code === 'budget_exceeded' ? 'budget_exceeded' : 'rate_limited'
  if (status === 503) return 'unavailable'
  if (status >= 500) return 'server'
  if (status >= 400) return 'bad_request'
  return 'unknown'
}

function headerValue(headers: unknown, name: string): string | null {
  if (!headers || typeof headers !== 'object') return null
  const h = headers as { get?: (n: string) => unknown } & Record<string, unknown>
  const value = typeof h.get === 'function' ? h.get(name) : (h[name] ?? h[name.toLowerCase()])
  return typeof value === 'string' && value ? value : null
}

const GENERIC: Record<ApiErrorKind, string> = {
  unauthorized: 'Authentication required or the credential was rejected.',
  forbidden: 'This action is not allowed.',
  not_found: 'The requested resource was not found.',
  bad_request: 'The request was not accepted.',
  payload_too_large: 'The request is too large.',
  validation: 'The request failed validation.',
  rejected: 'The provider rejected this request.',
  rate_limited: 'Too many requests. Please wait before trying again.',
  budget_exceeded: 'A spend limit was reached; no provider was called.',
  unavailable: 'The service is temporarily unavailable.',
  server: 'The server reported an error.',
  network: 'Cannot reach the backend API.', // replaced by networkMessage() where errors are built
  timeout: 'The request timed out.',
  cancelled: 'The request was cancelled.',
  unknown: 'An unexpected error occurred.',
}

export function normalizeError(error: unknown): ApiError {
  if (isApiError(error)) return error
  if (isCancel(error)) return new ApiError({ message: GENERIC.cancelled, status: null, code: 'cancelled', kind: 'cancelled' })
  if (isAxiosError(error)) {
    const response = error.response
    const requestId = headerValue(response?.headers, 'x-request-id')
    if (!response) {
      const timedOut = error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT'
      const kind: ApiErrorKind = error.code === 'ERR_CANCELED' ? 'cancelled' : timedOut ? 'timeout' : 'network'
      return new ApiError({ message: kind === 'network' ? networkMessage() : GENERIC[kind], status: null,
        code: kind === 'network' ? 'network_error' : kind, kind })
    }
    const status = response.status
    let body: unknown = response.data
    if (typeof body === 'string') {
      // text responses (e.g. /metrics) keep error bodies as raw JSON strings
      try {
        body = JSON.parse(body)
      } catch {
        body = null
      }
    }
    const envelope = isEnvelope(body) ? body.error : null
    if (!envelope && (status === 502 || status === 504)) {
      // The backend never answers 502/504 itself: a proxy/gateway (e.g. the Vite dev proxy) could not reach it.
      return new ApiError({ message: networkMessage(), status, code: 'backend_unreachable', kind: 'network', requestId })
    }
    const code = envelope?.code ?? (status === 404 ? 'not_found' : `http_${status}`)
    const kind = kindFor(status, code)
    const details = envelope?.details ?? null
    let retryAfterSeconds: number | null = null
    // 429 (rate limit) and 503 (e.g. rate_limiter_unavailable: Redis pool momentarily exhausted)
    // both carry Retry-After from the backend; other statuses never do.
    if (status === 429 || status === 503) {
      const fromDetails = (details as { retry_after_seconds?: unknown } | null)?.retry_after_seconds
      retryAfterSeconds = parseRetryAfter(headerValue(response.headers, 'retry-after')) ?? parseRetryAfter(fromDetails)
    }
    const message = typeof envelope?.message === 'string' && envelope.message ? envelope.message : GENERIC[kind]
    return new ApiError({ message, status, code, kind, details, retryAfterSeconds, requestId })
  }
  return new ApiError({ message: GENERIC.unknown, status: null, code: 'unknown', kind: 'unknown' })
}

/**
 * A 2xx response whose body does not match the expected schema. Rendered as "malformed" (never as
 * partial data). `problem` names a field path only; response values are never copied into it.
 */
export function malformedResponse(endpoint: string, problem: string, status: number | null): ApiError {
  return new ApiError({ message: `${endpoint} returned an unexpected body (${problem}).`, status,
    code: 'malformed_response', kind: 'unknown' })
}

/** Rejected before sending (e.g. a date that is not YYYY-MM-DD); no request was made. */
export function invalidParameters(message: string): ApiError {
  return new ApiError({ message, status: null, code: 'invalid_parameters', kind: 'validation' })
}

/** Short, user-facing guidance per error kind (shown under the backend's message). */
export function errorHint(error: ApiError): string | null {
  switch (error.kind) {
    case 'unauthorized':
      return 'Add or update the credential on the Settings page. Credentials are never stored in the app build.'
    case 'forbidden':
      return error.code === 'admin_disabled' ? 'Admin endpoints are disabled on the backend (ADMIN_API_TOKEN is not set).' : null
    case 'rate_limited':
      return error.retryAfterSeconds !== null ? `Try again in ${error.retryAfterSeconds}s.` : 'Try again shortly.'
    case 'budget_exceeded':
      return 'The backend refused the paid call before contacting any provider.'
    case 'unavailable':
      if (error.code === 'rate_limiter_unavailable') {
        return error.retryAfterSeconds !== null
          ? `The backend is busy. Try again in ${error.retryAfterSeconds}s.`
          : 'The backend is busy. Try again shortly.'
      }
      return error.code === 'database_unavailable'
        ? 'PostgreSQL is disabled or unreachable.'
        : error.code === 'budget_unavailable'
          ? 'The budget store (Redis) is unreachable, so paid calls are refused.'
          : error.retryAfterSeconds !== null
            ? `Try again in ${error.retryAfterSeconds}s.`
            : null
    case 'network':
      // Start-up instructions only make sense when the API is on this machine (local development);
      // a deployed site gets a neutral hint. (No build-time env flag: the bundle reads only VITE_API_BASE_URL.)
      return isLocalTarget()
        ? 'Start the backend (uvicorn app.main:app in backend/) and the Vite dev server, then retry.'
        : 'Check that the backend is running and reachable from this browser, then retry.'
    default:
      return null
  }
}
