/**
 * One function per backend endpoint (paths and fields verified against the OpenAPI schema).
 *
 * Protection (backend/app/api/deps.py):
 *   public            /health, /api/v1/classify, /api/v1/models
 *   chat key          POST /api/v1/chat (only when CHAT_AUTH_MODE=api_key)
 *   observability     /metrics, /api/v1/analytics/summary, /api/v1/cache/stats,
 *                     /api/v1/health/dependencies, /api/v1/settings, /api/v1/budget/{status,spend,refusals}
 *                     (only when OBSERVABILITY_API_TOKEN is set)
 *   admin (per call)  DELETE /api/v1/cache
 *
 * The Settings / Budget functions return `unknown` bodies on purpose: use the validated loaders in
 * src/lib/settings.ts and src/lib/budget.ts, which reject malformed responses instead of rendering them.
 */

import { API_BASE_URL, CHAT_TIMEOUT_MS } from '../config'
import { createApiClient, type ApiClient, type ApiResult } from './client'
import type {
  AnalyticsSummaryResponse, CacheClearResponse, CacheStatsResponse, ChatRequest, ChatResponse,
  ClassifyRequest, ClassifyResponse, DependenciesResponse, HealthResponse, ModelsResponse,
} from './types'

export interface RequestOptions {
  signal?: AbortSignal
}

/** UTC days, YYYY-MM-DD, inclusive. Omitted = backend default range. */
export interface DateRangeParams {
  startDate?: string
  endDate?: string
}

export interface RefusalQueryParams extends DateRangeParams {
  page?: number
  pageSize?: number
}

/** Query parameters with the backend's names; undefined values are not sent. */
export function budgetQueryParams(p: RefusalQueryParams): Record<string, string | number> {
  const out: Record<string, string | number> = {}
  if (p.startDate !== undefined) out.start_date = p.startDate
  if (p.endDate !== undefined) out.end_date = p.endDate
  if (p.page !== undefined) out.page = p.page
  if (p.pageSize !== undefined) out.page_size = p.pageSize
  return out
}

export interface ChatOptions extends RequestOptions {
  /** X-Request-ID to send (so the UI knows the id even if no response arrives). Must match [A-Za-z0-9._-]{1,64}. */
  requestId?: string
}

export function createEndpoints(client: ApiClient) {
  return {
    health: (o: RequestOptions = {}): Promise<ApiResult<HealthResponse>> =>
      client.request({ method: 'GET', url: '/health', signal: o.signal }),

    models: (o: RequestOptions = {}): Promise<ApiResult<ModelsResponse>> =>
      client.request({ method: 'GET', url: '/api/v1/models', signal: o.signal }),

    classify: (body: ClassifyRequest, o: RequestOptions = {}): Promise<ApiResult<ClassifyResponse>> =>
      client.request({ method: 'POST', url: '/api/v1/classify', data: body, signal: o.signal }),

    /** Paid when real providers are enabled: sent exactly once, never retried by the client. */
    chat: (body: ChatRequest, o: ChatOptions = {}): Promise<ApiResult<ChatResponse>> =>
      client.request({ method: 'POST', url: '/api/v1/chat', data: body, credential: 'chat',
        timeout: CHAT_TIMEOUT_MS, signal: o.signal, headers: o.requestId ? { 'X-Request-ID': o.requestId } : undefined }),

    analyticsSummary: (range: { start?: string; end?: string } = {}, o: RequestOptions = {}):
      Promise<ApiResult<AnalyticsSummaryResponse>> =>
      client.request({ method: 'GET', url: '/api/v1/analytics/summary', params: range,
        credential: 'observability', signal: o.signal }),

    cacheStats: (o: RequestOptions = {}): Promise<ApiResult<CacheStatsResponse>> =>
      client.request({ method: 'GET', url: '/api/v1/cache/stats', credential: 'observability', signal: o.signal }),

    dependencies: (o: RequestOptions = {}): Promise<ApiResult<DependenciesResponse>> =>
      client.request({ method: 'GET', url: '/api/v1/health/dependencies', credential: 'observability',
        signal: o.signal }),

    /** Prometheus text exposition format (parsed in a later step). */
    metricsText: (o: RequestOptions = {}): Promise<ApiResult<string>> =>
      client.request({ method: 'GET', url: '/metrics', credential: 'observability', responseType: 'text',
        headers: { Accept: 'text/plain' }, transformResponse: (d: unknown) => d, signal: o.signal }),

    /** Non-secret configuration view. Validate with loadSettings() (src/lib/settings.ts). */
    settings: (o: RequestOptions = {}): Promise<ApiResult<unknown>> =>
      client.request({ method: 'GET', url: '/api/v1/settings', credential: 'observability', signal: o.signal }),

    /** Daily limits and today's counted spend. Validate with loadBudgetStatus() (src/lib/budget.ts). */
    budgetStatus: (o: RequestOptions = {}): Promise<ApiResult<unknown>> =>
      client.request({ method: 'GET', url: '/api/v1/budget/status', credential: 'observability', signal: o.signal }),

    /** Persisted cost estimates per UTC day / provider / model. Validate with loadBudgetSpend(). */
    budgetSpend: (range: DateRangeParams = {}, o: RequestOptions = {}): Promise<ApiResult<unknown>> =>
      client.request({ method: 'GET', url: '/api/v1/budget/spend', params: budgetQueryParams(range),
        credential: 'observability', signal: o.signal }),

    /** Persisted budget refusals (paginated). Validate with loadBudgetRefusals(). */
    budgetRefusals: (query: RefusalQueryParams = {}, o: RequestOptions = {}): Promise<ApiResult<unknown>> =>
      client.request({ method: 'GET', url: '/api/v1/budget/refusals', params: budgetQueryParams(query),
        credential: 'observability', signal: o.signal }),

    /** Admin token is used for THIS request only; callers must discard it afterwards. */
    clearCache: (adminToken: string, resetStats = false): Promise<ApiResult<CacheClearResponse>> =>
      client.request({ method: 'DELETE', url: '/api/v1/cache', params: { reset_stats: resetStats }, adminToken }),
  }
}

export type Endpoints = ReturnType<typeof createEndpoints>

export const apiClient = createApiClient({ baseURL: API_BASE_URL })
export const api: Endpoints = createEndpoints(apiClient)
