/**
 * GET /api/v1/settings: runtime validation, validated loader and null-safe display helpers.
 *
 * The backend returns an explicit allow-list (no secret values, hosts, URLs or client names). This module
 * only checks that the body has exactly that shape; it never derives anything from credentials.
 */

import type { ApiResult } from '../api/client'
import type { Endpoints, RequestOptions } from '../api/endpoints'
import { malformedResponse } from '../api/errors'
import type { ProviderInfo, SettingsResponse } from '../api/types'
import { arr, bool, count, dict, explain, lit, nonNeg, nullable, num, obj, str, type Check } from './validate'

type S = SettingsResponse

const providerCheck: Check = obj<ProviderInfo>({
  provider: str, supported_for_real_calls: bool, credential_required: bool, credential_setting: nullable(str),
  credential_configured: nullable(bool), models: arr(str),
}, (p, path) => {
  // "Not required" must never look like "not configured", and a required credential is always true/false.
  if (p.credential_required === false && p.credential_configured !== null) return `${path}.credential_configured: must be null when no credential is required`
  if (p.credential_required === true && p.credential_configured === null) return `${path}.credential_configured: must be true/false when a credential is required`
  return null
})

export const settingsCheck: Check = obj<S>({
  app: obj<S['app']>({ name: str, version: str, environment: str, debug: bool, uptime_seconds: nonNeg }),
  mode: obj<S['mode']>({ mock_mode: bool, allow_real_provider_calls: bool, real_provider_calls_enabled: bool }),
  routing: obj<S['routing']>({
    strategy: str, tier_escalation: bool, model_registry: lit('built_in', 'custom_file'), fallback_enabled: bool,
    fallback_auto_tier: bool, fallback_on_override: bool, max_fallback_models: count, provider_timeout_seconds: nonNeg,
    provider_max_retries: count, provider_retry_backoff_seconds: nonNeg, default_max_output_tokens: count,
    real_max_output_tokens: count, health_backend: str, health_failure_threshold: count, health_cooldown_seconds: nonNeg,
  }),
  auth: obj<S['auth']>({
    chat_auth_mode: str, chat_clients_configured: count, observability_token_required: bool,
    admin_endpoints_enabled: bool, client_id_hashing: lit('hmac', 'sha256'),
  }),
  providers: arr(providerCheck),
  rate_limits: obj<S['rate_limits']>({
    enabled: bool, per_minute: nullable(count), per_hour: nullable(count), fail_open: bool, model_limits: dict(count),
    trust_proxy_headers: bool, trusted_proxies_configured: count,
  }),
  cache: obj<S['cache']>({
    enabled: bool, semantic_enabled: bool, semantic_allow_lexical: bool, ttl_seconds: count, similarity_threshold: num,
    max_entries_per_scope: count, embedding_provider: str, embedding_model: nullable(str),
    embedding_pricing_is_placeholder: bool,
  }),
  budget: obj<S['budget']>({
    configured: bool, applies_to: str, daily_usd: nullable(nonNeg), daily_usd_per_client: nullable(nonNeg),
    daily_tokens_per_client: nullable(count), max_cost_per_request_usd: nullable(nonNeg), fail_closed: bool,
    monthly_limit_supported: lit(false),
  }),
  persistence: obj<S['persistence']>({
    redis_enabled: bool, database_enabled: bool, database_auto_migrate: bool, persist_prompts: bool, persist_responses: bool,
  }),
  http: obj<S['http']>({
    docs_enabled: bool, security_headers_enabled: bool, hsts_enabled: bool, hsts_max_age_seconds: nullable(count),
    max_request_body_bytes: count, cors_enabled: bool, cors_origins_configured: count,
  }),
  observability: obj<S['observability']>({
    metrics_enabled: bool, access_log_enabled: bool, log_level: str, log_format: str, tracing_enabled: bool,
    tracing_exporter: str, tracing_sample_ratio: num, tracing_trust_incoming: bool,
  }),
  production_checks: obj<S['production_checks']>({
    enforced: bool,
    issues: arr(obj<S['production_checks']['issues'][number]>({ setting: str, severity: lit('error', 'warning'), message: str })),
  }),
})

export function isSettingsResponse(v: unknown): v is SettingsResponse {
  return explain(settingsCheck, v) === null
}

/** GET /api/v1/settings, rejecting any body that does not match the schema exactly. */
export async function loadSettings(api: Endpoints, o: RequestOptions = {}): Promise<ApiResult<SettingsResponse>> {
  const result = await api.settings(o)
  const problem = explain(settingsCheck, result.data)
  if (problem) throw malformedResponse('GET /api/v1/settings', problem, result.meta.status)
  return result as ApiResult<SettingsResponse>
}

// --------------------------------------------------------------------------- display helpers (pure)

export const NOT_CONFIGURED_TEXT = 'Not configured'
export const NOT_REQUIRED_TEXT = 'Not required'

export type CredentialStatus = 'configured' | 'not_configured' | 'not_required'

/** Never "configured" unless the backend said true; providers without a credential are "not required". */
export function credentialStatus(p: Pick<ProviderInfo, 'credential_required' | 'credential_configured'>): CredentialStatus {
  if (!p.credential_required || p.credential_configured === null) return 'not_required'
  return p.credential_configured ? 'configured' : 'not_configured'
}

export const CREDENTIAL_STATUS_TEXT: Record<CredentialStatus, string> = {
  configured: 'Configured', not_configured: NOT_CONFIGURED_TEXT, not_required: NOT_REQUIRED_TEXT,
}

/** Providers that need a key, and how many of those have one (providers without a key requirement are excluded). */
export function credentialSummary(providers: readonly ProviderInfo[]): { required: number; configured: number } {
  const required = providers.filter((p) => credentialStatus(p) !== 'not_required')
  return { required: required.length, configured: required.filter((p) => credentialStatus(p) === 'configured').length }
}

/** Seconds -> "3d 4h 5m" / "2h 3m" / "4m 5s". */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const d = Math.floor(s / 86_400)
  const h = Math.floor((s % 86_400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h ${m}m`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m ${s % 60}s`
}

export interface IssueCounts { errors: number; warnings: number }

export function issueCounts(issues: SettingsResponse['production_checks']['issues']): IssueCounts {
  return { errors: issues.filter((i) => i.severity === 'error').length, warnings: issues.filter((i) => i.severity === 'warning').length }
}

/** Errors first, then warnings; stable within each severity (the backend's order). */
export function sortedIssues(issues: SettingsResponse['production_checks']['issues']) {
  return [...issues.filter((i) => i.severity === 'error'), ...issues.filter((i) => i.severity === 'warning')]
}
