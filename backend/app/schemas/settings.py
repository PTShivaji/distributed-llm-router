"""Response schema for GET /api/v1/settings (read-only, non-secret configuration view).

Every model forbids extra fields, so the response can only ever contain the fields
listed here: an explicit allow-list. Values are copied field by field in
app/core/settings_view.py; the Settings object itself is never serialised.

Deliberately NOT part of this schema (and therefore impossible to return):
secret values or anything derived from them (prefixes, lengths, hashes), database /
Redis URLs, hosts, ports, user names, the tracing endpoint, the model registry file
path, chat client names, CORS origin values and trusted proxy addresses.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppInfo(_Strict):
    name: str
    version: str
    environment: str
    debug: bool
    uptime_seconds: float = Field(description="Seconds since this API process started (per instance).")


class ModeInfo(_Strict):
    mock_mode: bool
    allow_real_provider_calls: bool
    real_provider_calls_enabled: bool = Field(
        description="True only when LLM_MOCK_MODE=false AND ALLOW_REAL_PROVIDER_CALLS=true.")


class RoutingInfo(_Strict):
    strategy: str
    tier_escalation: bool
    model_registry: Literal["built_in", "custom_file"] = Field(
        description="Source of the model registry (the file path is not exposed).")
    fallback_enabled: bool
    fallback_auto_tier: bool
    fallback_on_override: bool
    max_fallback_models: int
    provider_timeout_seconds: float
    provider_max_retries: int
    provider_retry_backoff_seconds: float
    default_max_output_tokens: int
    real_max_output_tokens: int
    health_backend: str
    health_failure_threshold: int
    health_cooldown_seconds: float


class AuthInfo(_Strict):
    chat_auth_mode: str
    chat_clients_configured: int = Field(description="Number of chat API clients (names and keys are not exposed).")
    observability_token_required: bool
    admin_endpoints_enabled: bool = Field(description="True when ADMIN_API_TOKEN is set; otherwise admin endpoints are disabled.")
    client_id_hashing: Literal["hmac", "sha256"] = Field(
        description="hmac when CLIENT_ID_HASH_KEY is set, otherwise plain SHA-256.")


class ProviderInfo(_Strict):
    provider: str
    supported_for_real_calls: bool = Field(description="Provider is in the supported real-provider catalog.")
    credential_required: bool
    credential_setting: str | None = Field(description="Name of the environment variable that holds the key (never its value).")
    credential_configured: bool | None = Field(
        description="true/false when a credential is required; null when the provider needs none (mock, local).")
    models: list[str] = Field(description="Registered model ids served by this provider.")


class RateLimitInfo(_Strict):
    enabled: bool
    per_minute: int | None = Field(description="null = no per-minute limit.")
    per_hour: int | None = Field(description="null = no per-hour limit.")
    fail_open: bool
    model_limits: dict[str, int] = Field(description="Per-model requests/minute across all instances.")
    trust_proxy_headers: bool
    trusted_proxies_configured: int = Field(description="Number of trusted proxy entries (addresses not exposed).")


class CacheInfo(_Strict):
    enabled: bool
    semantic_enabled: bool
    semantic_allow_lexical: bool
    ttl_seconds: int
    similarity_threshold: float
    max_entries_per_scope: int
    embedding_provider: str
    embedding_model: str | None = Field(description="null when the local embedder is used (no model name).")
    embedding_pricing_is_placeholder: bool


class BudgetInfo(_Strict):
    configured: bool = Field(description="True when at least one spend limit is set.")
    applies_to: str
    daily_usd: float | None = Field(description="null = not configured.")
    daily_usd_per_client: float | None = Field(description="null = not configured.")
    daily_tokens_per_client: int | None = Field(description="null = not configured.")
    max_cost_per_request_usd: float | None = Field(description="null = not configured.")
    fail_closed: bool
    monthly_limit_supported: Literal[False] = Field(
        default=False, description="The backend has no monthly spend limit; only daily limits exist.")


class PersistenceInfo(_Strict):
    redis_enabled: bool
    database_enabled: bool
    database_auto_migrate: bool
    persist_prompts: bool
    persist_responses: bool


class HttpInfo(_Strict):
    docs_enabled: bool = Field(description="Effective value (docs are off by default in production).")
    security_headers_enabled: bool
    hsts_enabled: bool
    hsts_max_age_seconds: int | None = Field(description="null when HSTS is disabled.")
    max_request_body_bytes: int
    cors_enabled: bool
    cors_origins_configured: int = Field(description="Number of allowed origins (values not exposed).")


class ObservabilityInfo(_Strict):
    metrics_enabled: bool
    access_log_enabled: bool
    log_level: str
    log_format: str
    tracing_enabled: bool
    tracing_exporter: str
    tracing_sample_ratio: float
    tracing_trust_incoming: bool


class ConfigIssueOut(_Strict):
    setting: str = Field(description="Environment variable name (no client names or values).")
    severity: Literal["error", "warning"]
    message: str = Field(description="Fixed description of the problem; never contains values.")


class ProductionChecks(_Strict):
    enforced: bool = Field(description="True in production (errors abort startup) and staging (logged).")
    issues: list[ConfigIssueOut] = Field(
        description="Result of the production configuration checks for the CURRENT settings. "
                    "Outside production/staging these show what would block a production start.")


class SettingsResponse(_Strict):
    app: AppInfo
    mode: ModeInfo
    routing: RoutingInfo
    auth: AuthInfo
    providers: list[ProviderInfo]
    rate_limits: RateLimitInfo
    cache: CacheInfo
    budget: BudgetInfo
    persistence: PersistenceInfo
    http: HttpInfo
    observability: ObservabilityInfo
    production_checks: ProductionChecks
