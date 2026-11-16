/**
 * Pure helpers for the Chat Playground. Labels and explanations only: every value shown in the
 * UI comes from the backend response (or its error envelope); nothing here invents data.
 */

import type { RateLimitInfo } from '../api/client'
import type { ApiError } from '../api/errors'
import type { ChatRequest, ModelOut } from '../api/types'
import { formatInteger } from './format'

/** Same limit as the backend's ChatRequest.query (1..10,000 characters). The backend still validates. */
export const MAX_QUERY_LENGTH = 10_000

/** Example prompts for the empty state. They only fill the input; the backend decides the routing. */
export const EXAMPLE_PROMPTS: readonly { label: string; prompt: string }[] = [
  { label: 'Quick fact', prompt: 'What is the capital of France?' },
  { label: 'Explanation', prompt: 'Explain how Python variables and references work, with a short example.' },
  { label: 'Coding', prompt: 'Write a Python function that reverses a linked list and explain its time complexity.' },
  { label: 'System design', prompt: 'Design a distributed rate limiter for a multi-region API. Compare token bucket and sliding window approaches and analyse the trade-offs.' },
]

export function buildChatRequest(input: { query: string; model: string | null; useCache: boolean }): ChatRequest {
  const body: ChatRequest = { query: input.query, use_cache: input.useCache }
  if (input.model) body.model = input.model
  return body
}

export type QueryProblem = 'empty' | 'too_long' | null

export function queryProblem(query: string): QueryProblem {
  if (query.trim() === '') return 'empty'
  if (query.length > MAX_QUERY_LENGTH) return 'too_long'
  return null
}

/** Only registered AND enabled models can be chosen as an override. */
export function selectableModels(models: readonly ModelOut[]): ModelOut[] {
  return models.filter((m) => m.enabled)
}

/** Returns the override if it is still a registered, enabled model; otherwise null (automatic routing). */
export function validOverride(selected: string | null, models: readonly ModelOut[] | null): string | null {
  if (!selected || !models) return null
  return models.some((m) => m.id === selected && m.enabled) ? selected : null
}

const CACHE_LABELS: Record<string, string> = {
  disabled: 'Cache disabled',
  bypass: 'Cache bypassed',
  miss: 'Cache miss',
  hit_exact: 'Exact cache hit',
  hit_semantic: 'Semantic cache hit',
  error: 'Cache error',
}

export function cacheLabel(status: string): string {
  return CACHE_LABELS[status] ?? status
}

export function isCacheHit(status: string): boolean {
  return status === 'hit_exact' || status === 'hit_semantic'
}

const USAGE_SOURCES: Record<string, string> = {
  provider_reported: 'Reported by the provider',
  estimated: 'Estimated by the router',
  partially_estimated: 'Partly estimated by the router',
  unavailable: 'Not available',
  cache_hit: 'Served from cache (no provider call)',
}

export function usageSourceLabel(source: string): string {
  return USAGE_SOURCES[source] ?? source
}

export function intentLabel(intent: string): string {
  return intent.replace(/_/g, ' ')
}

export interface ChatErrorView {
  title: string
  /** What happened and what the user can do. Shown under the backend's own message. */
  explanation: string | null
  /** The model override is the problem (400 unknown_model / model_disabled). */
  modelProblem: boolean
  /** Offer the chat credential prompt (401). */
  needsCredential: boolean
}

/**
 * One-line quota summary from the X-RateLimit-* response headers (backend window names: "minute",
 * "hour"). null when the backend sent no rate-limit headers (limits disabled, or not reported).
 */
export function rateLimitSummary(rl: RateLimitInfo | null | undefined): string | null {
  if (!rl || rl.limit === null) return null
  const left = rl.remaining === null ? 'unknown' : formatInteger(rl.remaining)
  let text = `${left} of ${formatInteger(rl.limit)} requests left`
  if (rl.window) text += ` this ${rl.window}`
  if (rl.resetSeconds !== null) text += `, resets in ${formatInteger(rl.resetSeconds)}s`
  return text
}

/** Maps the backend's error code / kind to a precise title. Rate limits and budget refusals are never conflated. */
export function describeChatError(error: ApiError): ChatErrorView {
  const view = (title: string, explanation: string | null, extra: Partial<ChatErrorView> = {}): ChatErrorView =>
    ({ title, explanation, modelProblem: false, needsCredential: false, ...extra })
  switch (error.code) {
    case 'unknown_model':
      return view('Model not registered', 'Choose another model or use automatic routing.', { modelProblem: true })
    case 'model_disabled':
      return view('Model disabled', 'This model is disabled in the registry. Choose another model or use automatic routing.', { modelProblem: true })
    case 'unauthorized':
      return view('Chat API key required', 'The backend requires a chat API key (CHAT_AUTH_MODE=api_key).', { needsCredential: true })
    case 'request_too_large':
      return view('Request too large', 'The request body exceeds the backend size limit. Shorten the prompt.')
    case 'validation_error':
      return view('Request failed validation', 'Fix the request and send it again.')
    case 'request_rejected_by_provider':
      return view('Rejected by the provider', 'The provider refused this request (for example a content policy). Fallback is not attempted for rejections.')
    case 'rate_limit_exceeded':
      return view('Rate limit reached', 'Too many requests from this client. Wait before sending again.')
    case 'budget_exceeded':
      return view('Spend limit reached', 'A budget safeguard refused the paid call before any provider was contacted. This is not a rate limit; waiting a few seconds will not help.')
    case 'budget_unavailable':
      return view('Budget store unavailable', 'Spend limits cannot be checked (Redis unreachable), so paid calls are refused.')
    case 'all_models_failed':
      return view('All models failed', 'Every candidate model (including fallbacks) failed. See the attempts below.')
    case 'no_model_available':
      return view('No model available', 'No enabled, healthy model can serve this request.')
    case 'rate_limiter_unavailable':
      return view('Backend busy, retry shortly',
        'The rate limiter was momentarily saturated, so the request was refused before any model was called. Nothing was charged; wait for the countdown, then retry.')
    default:
      break
  }
  switch (error.kind) {
    case 'unauthorized':
      return view('Chat API key required', 'The backend rejected the request credentials.', { needsCredential: true })
    case 'payload_too_large':
      return view('Request too large', 'Shorten the prompt.')
    case 'validation':
      return view('Request failed validation', null)
    case 'rate_limited':
      return view('Rate limit reached', null)
    case 'network':
      return view('Backend unreachable', 'The request may not have reached the backend.')
    case 'timeout':
      return view('Request timed out', 'The backend may still have processed (and billed) this request. Check before sending it again.')
    case 'unavailable':
      return view('Service unavailable', null)
    case 'server':
      return view('Server error', null)
    default:
      return view('Request failed', null)
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)

export interface ErrorAttempt {
  model: string
  outcome: string | null
  reason: string | null
}

/** Attempts listed in all_models_failed / budget_* error details (only well-formed entries). */
export function errorAttempts(details: unknown): ErrorAttempt[] {
  if (!isRecord(details) || !Array.isArray(details.attempts)) return []
  return details.attempts.flatMap((a): ErrorAttempt[] => {
    if (!isRecord(a) || !str(a.model)) return []
    return [{ model: str(a.model)!, outcome: str(a.outcome), reason: str(a.error_kind) ?? str(a.reason) }]
  })
}

/** Budget refusal reasons from budget_exceeded / budget_unavailable details. */
export function budgetReasons(details: unknown): string[] {
  if (!isRecord(details) || !Array.isArray(details.reasons)) return []
  return details.reasons.filter((r): r is string => typeof r === 'string' && r !== '')
}

/** Field messages from a 422 validation_error (the backend never echoes submitted values). */
export function validationMessages(details: unknown): string[] {
  if (!Array.isArray(details)) return []
  return details.flatMap((d) => {
    if (!isRecord(d) || !str(d.msg)) return []
    const loc = Array.isArray(d.loc) ? d.loc.filter((p) => p !== 'body').join('.') : ''
    return [loc ? `${loc}: ${d.msg as string}` : (d.msg as string)]
  })
}
