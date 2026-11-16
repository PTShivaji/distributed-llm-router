"""Read-only, non-secret view of the effective configuration (GET /api/v1/settings).

Safety model:
  1. Explicit allow-list: every field is copied one by one into strict response models
     (app/schemas/settings.py, extra="forbid"). Settings is never dumped or serialised.
  2. Secrets are reduced to booleans or counts at most ("is a credential configured?",
     "how many chat clients?"). No value, prefix, length or hash is ever derived.
  3. Production-check issues are sanitised: client names are stripped from setting
     names, and every issue about a secret setting gets one fixed message (the original
     messages can mention secret lengths or that the development default password is in use).
  4. Last line of defence: the serialised response is scanned for every configured secret
     value; if one were ever present, the view is refused (SettingsLeakError -> HTTP 500,
     the generic error envelope) instead of being returned.
"""

import json
import logging
import re

from app.core.config import Settings
from app.core.logging import log_event
from app.core.production import check_production_settings
from app.providers.catalog import SUPPORTED_PROVIDERS
from app.routing.registry import ModelRegistry
from app.schemas.settings import (
    AppInfo,
    AuthInfo,
    BudgetInfo,
    CacheInfo,
    ConfigIssueOut,
    HttpInfo,
    ModeInfo,
    ObservabilityInfo,
    PersistenceInfo,
    ProductionChecks,
    ProviderInfo,
    RateLimitInfo,
    RoutingInfo,
    SettingsResponse,
)

logger = logging.getLogger("app.settings_view")

# Settings whose production-check messages may describe the secret itself.
SECRET_SETTINGS = frozenset({
    "ADMIN_API_TOKEN", "OBSERVABILITY_API_TOKEN", "CLIENT_ID_HASH_KEY", "CHAT_API_KEYS",
    "POSTGRES_PASSWORD", "REDIS_PASSWORD",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
})
SECRET_ISSUE_MESSAGE = ("does not meet the production requirements for this secret "
                        "(missing, too weak, a known default or reused); the value is never shown")
_SUFFIX = re.compile(r"\[.*\]$")  # e.g. CHAT_API_KEYS[<client name>] -> CHAT_API_KEYS
# Secrets shorter than this are not scanned for (too likely to match ordinary words);
# every secret enforced in production is far longer (16+ characters).
MIN_SCANNED_SECRET_LENGTH = 8


class SettingsLeakError(RuntimeError):
    """A configured secret value was found in the settings view. Never carries the value."""


def _none_if_zero(value: float | int) -> float | int | None:
    return value if value else None


def _issues(s: Settings) -> list[ConfigIssueOut]:
    out: list[ConfigIssueOut] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in check_production_settings(s):
        setting = _SUFFIX.sub("", issue.setting)
        message = SECRET_ISSUE_MESSAGE if setting in SECRET_SETTINGS else issue.message
        key = (setting, issue.severity, message)
        if key not in seen:  # several chat clients collapse into one CHAT_API_KEYS entry
            seen.add(key)
            out.append(ConfigIssueOut(setting=setting, severity=issue.severity, message=message))
    return out


def _providers(s: Settings, registry: ModelRegistry) -> list[ProviderInfo]:
    configured = s.provider_api_keys()  # names only are used below; values are never read
    models_by_provider: dict[str, list[str]] = {}
    for m in registry.all():
        models_by_provider.setdefault(m.provider, []).append(m.id)
    names = list(SUPPORTED_PROVIDERS) + sorted(p for p in models_by_provider if p not in SUPPORTED_PROVIDERS)
    out = []
    for name in names:
        spec = SUPPORTED_PROVIDERS.get(name)
        required = bool(spec and spec.key_setting)
        out.append(ProviderInfo(
            provider=name,
            supported_for_real_calls=spec is not None,
            credential_required=required,
            credential_setting=spec.key_setting if spec else None,
            credential_configured=(name in configured) if required else None,
            models=sorted(models_by_provider.get(name, [])),
        ))
    return out


def build_settings_view(s: Settings, registry: ModelRegistry, *, uptime_seconds: float) -> SettingsResponse:
    budget_configured = bool(s.budget_daily_usd or s.budget_daily_usd_per_client
                             or s.budget_daily_tokens_per_client or s.budget_max_cost_per_request_usd)
    view = SettingsResponse(
        app=AppInfo(name=s.app_name, version=s.app_version, environment=s.environment, debug=s.debug,
                    uptime_seconds=round(uptime_seconds, 3)),
        mode=ModeInfo(mock_mode=s.llm_mock_mode, allow_real_provider_calls=s.allow_real_provider_calls,
                      real_provider_calls_enabled=s.real_provider_calls_enabled),
        routing=RoutingInfo(
            strategy=s.routing_strategy, tier_escalation=s.routing_tier_escalation,
            model_registry="custom_file" if s.model_registry_file else "built_in",
            fallback_enabled=s.fallback_enabled, fallback_auto_tier=s.fallback_auto_tier,
            fallback_on_override=s.fallback_on_override, max_fallback_models=s.max_fallback_models,
            provider_timeout_seconds=s.provider_timeout_seconds, provider_max_retries=s.provider_max_retries,
            provider_retry_backoff_seconds=s.provider_retry_backoff_seconds,
            default_max_output_tokens=s.default_max_output_tokens, real_max_output_tokens=s.real_max_output_tokens,
            health_backend=s.health_backend, health_failure_threshold=s.health_failure_threshold,
            health_cooldown_seconds=s.health_cooldown_seconds,
        ),
        auth=AuthInfo(
            chat_auth_mode=s.chat_auth_mode, chat_clients_configured=len(s.chat_api_keys),
            observability_token_required=s.observability_api_token is not None,
            admin_endpoints_enabled=s.admin_api_token is not None,
            client_id_hashing="hmac" if s.client_id_hash_key is not None else "sha256",
        ),
        providers=_providers(s, registry),
        rate_limits=RateLimitInfo(
            enabled=s.rate_limit_enabled, per_minute=_none_if_zero(s.rate_limit_per_minute),
            per_hour=_none_if_zero(s.rate_limit_per_hour), fail_open=s.rate_limit_fail_open,
            model_limits=dict(sorted(s.model_rate_limits.items())), trust_proxy_headers=s.trust_proxy_headers,
            trusted_proxies_configured=len(s.trusted_proxies),
        ),
        cache=CacheInfo(
            enabled=s.cache_enabled, semantic_enabled=s.cache_semantic_enabled,
            semantic_allow_lexical=s.cache_semantic_allow_lexical, ttl_seconds=s.cache_ttl_seconds,
            similarity_threshold=s.cache_similarity_threshold, max_entries_per_scope=s.cache_max_entries_per_scope,
            embedding_provider=s.embedding_provider,
            embedding_model=(s.embedding_model or None) if s.embedding_provider != "local" else None,
            embedding_pricing_is_placeholder=s.embedding_pricing_is_placeholder,
        ),
        budget=BudgetInfo(
            configured=budget_configured,
            applies_to="real provider calls and paid embeddings only (mock calls never use the budget)",
            daily_usd=_none_if_zero(s.budget_daily_usd), daily_usd_per_client=_none_if_zero(s.budget_daily_usd_per_client),
            daily_tokens_per_client=_none_if_zero(s.budget_daily_tokens_per_client),
            max_cost_per_request_usd=_none_if_zero(s.budget_max_cost_per_request_usd),
            fail_closed=s.budget_fail_closed,
        ),
        persistence=PersistenceInfo(
            redis_enabled=s.redis_enabled, database_enabled=s.database_enabled,
            database_auto_migrate=s.database_auto_migrate, persist_prompts=s.persist_prompts,
            persist_responses=s.persist_responses,
        ),
        http=HttpInfo(
            docs_enabled=s.docs_effective, security_headers_enabled=s.security_headers_enabled,
            hsts_enabled=s.hsts_enabled, hsts_max_age_seconds=s.hsts_max_age_seconds if s.hsts_enabled else None,
            max_request_body_bytes=s.max_request_body_bytes, cors_enabled=bool(s.cors_allowed_origins),
            cors_origins_configured=len(s.cors_allowed_origins),
        ),
        observability=ObservabilityInfo(
            metrics_enabled=s.metrics_enabled, access_log_enabled=s.access_log_enabled, log_level=s.log_level,
            log_format=s.log_format, tracing_enabled=s.tracing_enabled, tracing_exporter=s.tracing_exporter,
            tracing_sample_ratio=s.tracing_sample_ratio, tracing_trust_incoming=s.tracing_trust_incoming,
        ),
        production_checks=ProductionChecks(enforced=s.environment in ("production", "staging"), issues=_issues(s)),
    )
    assert_no_secret_values(view, s)
    return view


def assert_no_secret_values(view: SettingsResponse, s: Settings) -> None:
    """Refuse the view if any configured secret value appears anywhere in its JSON form."""
    text = json.dumps(view.model_dump(mode="json"))
    leaked = sum(1 for v in s.secret_values() if len(v) >= MIN_SCANNED_SECRET_LENGTH and v in text)
    if leaked:
        log_event(logger, "settings_view.secret_detected", logging.ERROR, count=leaked)  # count only, never values
        raise SettingsLeakError("settings view refused: it would contain a configured secret value")
