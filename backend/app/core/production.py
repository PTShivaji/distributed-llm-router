"""Production configuration validation (runs at startup, before anything is served).

ENVIRONMENT=production  -> any ERROR aborts startup with ProductionConfigError.
ENVIRONMENT=staging     -> the same checks are logged as warnings (no abort).
development / test      -> unchanged behaviour: only invariant errors that would
                           make the app misbehave anywhere (none today) abort.

Messages name the SETTING and the problem only. Secret values are never
included in messages, logs or exceptions.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from app.core.config import Settings
from app.core.logging import log_event

logger = logging.getLogger("app.production")

DEFAULT_POSTGRES_PASSWORD = "router_password"
MIN_TOKEN_LENGTH = 32
MIN_PASSWORD_LENGTH = 16
PLACEHOLDER_MARKERS = ("change", "replace", "example", "placeholder", "your-", "your_", "xxx", "todo", "secret",
                       "password", "default", "generate", "<", ">")

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class ConfigIssue:
    setting: str
    severity: Severity
    message: str

    def __str__(self) -> str:
        return f"{self.setting}: {self.message}"


class ProductionConfigError(RuntimeError):
    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = issues
        lines = "\n".join(f"  - {i}" for i in issues)
        super().__init__(f"Refusing to start: insecure production configuration ({len(issues)} problem(s)):\n{lines}")


def _secret_problem(value: str | None, *, min_length: int, kind: str) -> str | None:
    """Return a problem description WITHOUT the value, or None if acceptable."""
    if not value:
        return f"required in production ({kind}, at least {min_length} characters)"
    if len(value) < min_length:
        return f"too short ({len(value)} characters; at least {min_length} required)"
    lowered = value.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return "looks like a placeholder value; generate a random secret"
    if len(set(value)) < 8:
        return "too little variety; generate a random secret"
    return None


def _value(secret) -> str | None:
    return secret.get_secret_value() if secret is not None else None


def check_production_settings(s: Settings) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []

    def err(setting: str, message: str) -> None:
        issues.append(ConfigIssue(setting, "error", message))

    def warn(setting: str, message: str) -> None:
        issues.append(ConfigIssue(setting, "warning", message))

    # --- secrets -------------------------------------------------------
    for setting, secret, min_len, kind in (
        ("ADMIN_API_TOKEN", s.admin_api_token, MIN_TOKEN_LENGTH, "admin bearer token"),
        ("OBSERVABILITY_API_TOKEN", s.observability_api_token, MIN_TOKEN_LENGTH,
         "protects /metrics, /api/v1/analytics, /api/v1/cache/stats, /health/dependencies"),
        ("CLIENT_ID_HASH_KEY", s.client_id_hash_key, MIN_TOKEN_LENGTH, "HMAC key for client identifiers"),
    ):
        if problem := _secret_problem(_value(secret), min_length=min_len, kind=kind):
            err(setting, problem)

    admin, obs = _value(s.admin_api_token), _value(s.observability_api_token)
    if admin and obs and admin == obs:
        err("OBSERVABILITY_API_TOKEN", "must differ from ADMIN_API_TOKEN (separate credentials)")
    if s.chat_auth_mode != "api_key":
        err("CHAT_AUTH_MODE", "must be 'api_key' in production (/api/v1/chat would be open to anyone)")
    for name, key in s.chat_api_keys.items():
        if problem := _secret_problem(_value(key), min_length=MIN_TOKEN_LENGTH, kind="chat API key"):
            err(f"CHAT_API_KEYS[{name}]", problem)

    pg = _value(s.postgres_password)
    if s.database_enabled:
        if pg == DEFAULT_POSTGRES_PASSWORD:
            err("POSTGRES_PASSWORD", "the development default password must not be used in production")
        elif problem := _secret_problem(pg, min_length=MIN_PASSWORD_LENGTH, kind="database password"):
            err("POSTGRES_PASSWORD", problem)
    if s.redis_enabled and (problem := _secret_problem(_value(s.redis_password), min_length=MIN_PASSWORD_LENGTH,
                                                         kind="Redis password (requirepass)")):
        err("REDIS_PASSWORD", problem)

    # --- unsafe switches -------------------------------------------------
    if s.debug:
        err("DEBUG", "must be false in production (debug mode can expose internals)")
    if "*" in s.cors_allowed_origins:
        err("CORS_ALLOWED_ORIGINS", "wildcard '*' is not allowed in production; list exact origins")
    for origin in s.cors_allowed_origins:
        if origin != "*" and not origin.startswith("https://"):
            warn("CORS_ALLOWED_ORIGINS", "non-HTTPS origin configured")
            break
    if s.trust_proxy_headers and not s.trusted_proxies:
        err("TRUSTED_PROXIES", "TRUST_PROXY_HEADERS=true requires at least one trusted proxy IP/CIDR")
    if "0.0.0.0/0" in s.trusted_proxies or "::/0" in s.trusted_proxies:
        err("TRUSTED_PROXIES", "trusting every address lets any client spoof X-Forwarded-For")
    if s.cache_semantic_allow_lexical:
        err("CACHE_SEMANTIC_ALLOW_LEXICAL", "lexical semantic matching can serve wrong cached answers (step 6A)")

    # --- recommended hardening (warnings) --------------------------------
    if s.docs_enabled:
        warn("DOCS_ENABLED", "interactive API docs are publicly reachable")
    if not s.security_headers_enabled:
        warn("SECURITY_HEADERS_ENABLED", "security headers are disabled")
    if not s.hsts_enabled:
        warn("HSTS_ENABLED", "enable once the API is served over HTTPS")
    if not s.rate_limit_enabled:
        warn("RATE_LIMIT_ENABLED", "rate limiting is disabled")
    elif s.rate_limit_fail_open:
        warn("RATE_LIMIT_FAIL_OPEN", "requests are allowed without limits while Redis is unreachable")
    if s.tracing_trust_incoming:
        warn("TRACING_TRUST_INCOMING", "only enable behind a gateway that sets traceparent itself")
    if s.persist_prompts or s.persist_responses:
        warn("PERSIST_PROMPTS/PERSIST_RESPONSES", "raw prompt/response text will be stored in PostgreSQL")
    if s.llm_mock_mode:
        warn("LLM_MOCK_MODE", "mock mode is on: responses are not real model output")
    elif s.real_provider_calls_enabled:
        if not s.budget_daily_usd_per_client:
            warn("BUDGET_DAILY_USD_PER_CLIENT", "one client can consume the whole global daily budget")
        if not s.budget_fail_closed:
            warn("BUDGET_FAIL_CLOSED", "paid calls continue without budget enforcement while Redis is unreachable")
    if s.log_level == "DEBUG":
        warn("LOG_LEVEL", "DEBUG logging in production increases data exposure and volume")
    return issues


def enforce_production_settings(s: Settings) -> list[ConfigIssue]:
    """Validate at startup. Raises ProductionConfigError in production if any error exists."""
    if s.environment not in ("production", "staging"):
        return []
    issues = check_production_settings(s)
    errors = [i for i in issues if i.severity == "error"]
    for issue in issues:
        level = logging.ERROR if (issue.severity == "error" and s.environment == "production") else logging.WARNING
        log_event(logger, "config.production_check", level, setting=issue.setting, severity=issue.severity,
                  environment=s.environment, problem=issue.message)
    if s.environment == "production" and errors:
        raise ProductionConfigError(errors)
    return issues
