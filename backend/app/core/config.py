"""Application configuration loaded from environment variables / .env file.

All settings have safe defaults so the app starts with zero configuration and
no API keys. Values are read (in priority order) from:
  1. Real environment variables
  2. The project-root .env file (if present)
  3. The defaults defined below
"""

import os
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.providers.base import MockFailureMode

# <project-root>/backend/app/core/config.py -> parents[3] == <project-root>
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# LLMR_DISABLE_ENV_FILE=1 makes the app ignore the project-root .env entirely.
# The test suite sets it (tests/conftest.py) so a developer's real .env can
# never influence - or be read by - automated tests.
ENV_FILE_DISABLED = os.environ.get("LLMR_DISABLE_ENV_FILE") == "1"
ENV_FILE: Path | None = None if ENV_FILE_DISABLED else PROJECT_ROOT / ".env"

CHAT_CLIENT_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
MIN_CHAT_KEY_LENGTH = 16  # every environment; production requires >= 32 (app/core/production.py)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # ignore unrelated vars (e.g. Docker-only ones) in .env
        # Validation errors must never echo raw input (it can contain keys/passwords).
        hide_input_in_errors=True,
    )

    # --- Application ---
    app_name: str = "Distributed LLM Router"
    app_version: str = "0.1.0"
    environment: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["text", "json"] = "text"  # json = one JSON object per line
    access_log_enabled: bool = True  # structured http.request events (hashed client, no IPs)
    # Prometheus metrics. GET /metrics is served when enabled; if
    # OBSERVABILITY_API_TOKEN is set it requires "Authorization: Bearer <token>".
    metrics_enabled: bool = True
    observability_api_token: SecretStr | None = None
    # OpenTelemetry tracing - DISABLED by default.
    # exporter: none (spans created, ids in logs, nothing exported) | memory (tests/debug)
    #           | console (stdout) | otlp (needs requirements-observability.txt)
    tracing_enabled: bool = False
    tracing_exporter: Literal["none", "memory", "console", "otlp"] = "none"
    tracing_service_name: str = Field(default="llm-router", pattern=r"^[A-Za-z0-9._-]{1,64}$")
    tracing_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    # Accept an incoming W3C traceparent as PARENT (only behind a trusted gateway).
    # false -> new trace per request; the incoming context is kept as a span link.
    tracing_trust_incoming: bool = False
    tracing_otlp_endpoint: str | None = None  # e.g. http://localhost:4318/v1/traces
    # HMAC key for client identifiers in logs, rate-limit keys and DB rows.
    # Unset -> plain SHA-256 (reversible for IPv4 by brute force); set in production.
    client_id_hash_key: SecretStr | None = None
    api_v1_prefix: str = "/api/v1"

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_db: int = Field(default=0, ge=0)
    redis_password: SecretStr | None = None  # SecretStr: masked in repr/dumps/logs

    # --- PostgreSQL ---
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5433, ge=1, le=65535)  # Docker host port
    postgres_user: str = "router"
    postgres_password: SecretStr = SecretStr("router_password")  # dev default; rejected in production
    postgres_db: str = "llm_router"

    # --- Query classifier ---
    # score <  simple_threshold  -> SIMPLE
    # score >= complex_threshold -> COMPLEX, otherwise MEDIUM
    classifier_simple_threshold: float = Field(default=0.28, gt=0, lt=1)
    classifier_complex_threshold: float = Field(default=0.55, gt=0, lt=1)

    # --- LLM routing (Phase 3) ---
    # Mock mode: every model is served by the deterministic MockProvider.
    llm_mock_mode: bool = True
    # JSON registry file. Relative paths are resolved from the project root.
    # Unset -> built-in mock registry (app/routing/default_models.json).
    model_registry_file: Path | None = None
    routing_strategy: Literal["priority", "lowest_cost"] = "priority"
    # If a tier has no usable model, try other tiers (higher first, then lower).
    routing_tier_escalation: bool = True

    # --- Fallback & retries ---
    fallback_enabled: bool = True
    # After a model's configured fallbacks, also try other models (same tier, then others).
    fallback_auto_tier: bool = True
    # Respect an explicit model override strictly (no fallback) unless enabled.
    fallback_on_override: bool = False
    max_fallback_models: int = Field(default=3, ge=0, le=10)
    provider_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    provider_max_retries: int = Field(default=1, ge=0, le=5)
    provider_retry_backoff_seconds: float = Field(default=0.25, ge=0, le=10)
    default_max_output_tokens: int = Field(default=1024, ge=1, le=200_000)
    # Estimate tokens (~4 chars/token) when a provider doesn't report usage.
    estimate_missing_usage: bool = True

    # --- Provider health ---
    health_failure_threshold: int = Field(default=3, ge=1, le=100)
    health_cooldown_seconds: float = Field(default=30.0, ge=0, le=3600)

    # --- Mock provider ---
    mock_latency_ms: int = Field(default=0, ge=0, le=60_000)
    # Simulated failures for demos, e.g. MOCK_FAILURES={"mock-advanced": "timeout"}
    mock_failures: dict[str, MockFailureMode] = Field(default_factory=dict)

    # --- Provider API keys (only needed when LLM_MOCK_MODE=false) ---
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    mistral_api_key: SecretStr | None = None

    # ===================== Phase 4: distributed infrastructure =====================
    # --- Redis connection (one shared pool) ---
    redis_enabled: bool = True
    redis_key_prefix: str = Field(default="llmr:", pattern=r"^[A-Za-z0-9:_.-]{1,64}$")
    redis_connect_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    redis_socket_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    redis_max_connections: int = Field(default=50, ge=1, le=10_000)
    redis_health_check_interval_seconds: int = Field(default=30, ge=0, le=3600)

    # --- Semantic cache ---
    cache_enabled: bool = True
    # Approximate (embedding-similarity) hits. OFF by default since Phase 5 step 6A:
    # measured with the local lexical embedder, 14/16 meaning-changing edits
    # (negations, numbers, dates, entities, versions) still scored >= 0.92.
    # Even when enabled, they need a meaning-aware embedder (EMBEDDING_PROVIDER=litellm)
    # unless CACHE_SEMANTIC_ALLOW_LEXICAL=true is set explicitly (unsafe; see README).
    # Exact-match caching is unaffected.
    cache_semantic_enabled: bool = False
    cache_semantic_allow_lexical: bool = False
    cache_ttl_seconds: int = Field(default=3600, ge=1, le=30 * 86400)
    cache_similarity_threshold: float = Field(default=0.92, gt=0, le=1)
    cache_max_entries_per_scope: int = Field(default=1000, ge=1, le=100_000)
    # Phase 11.1 (H1): bounded cache memory. Cached answers occupy at most
    # CACHE_MAX_ENTRIES x CACHE_MAX_ENTRY_BYTES (+ index overhead) across ALL scopes and instances
    # (defaults: 5000 x 32 KiB = 160 MiB, well below the production Redis maxmemory of 384 MiB).
    cache_max_entries: int = Field(default=5000, ge=1, le=1_000_000)
    cache_max_entry_bytes: int = Field(default=32_768, ge=1024, le=10 * 1_048_576)  # larger answers: not cached
    # Bounded scope cardinality: only these effective max_output_tokens values (plus the model's own
    # maximum and DEFAULT_MAX_OUTPUT_TOKENS) and temperatures on a 0.1 grid are cached. Other requests
    # are answered normally with their exact parameters but skip the cache (status "bypass").
    cache_cacheable_max_output_tokens: list[int] = Field(default_factory=lambda: [256, 512, 1024, 2048, 4096])
    cache_lock_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    cache_lock_wait_seconds: float = Field(default=5.0, ge=0, le=120)

    # --- Embeddings ---
    embedding_provider: Literal["local", "litellm"] = "local"
    embedding_model: str = ""  # LiteLLM embedding model, e.g. "openai/text-embedding-3-small"
    embedding_dimensions: int = Field(default=256, ge=16, le=8192)  # local embedder only
    embedding_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    # Step 8A: EMBEDDING_PROVIDER=litellm is a PAID call. It requires ALLOW_REAL_PROVIDER_CALLS=true
    # (even in mock mode), BUDGET_DAILY_USD > 0, the provider's key and VERIFIED pricing below,
    # or the app refuses to start. Every call is reserved against the shared budget.
    embedding_input_cost_per_million: float = Field(default=0.0, ge=0, le=10_000, allow_inf_nan=False)
    embedding_pricing_source: str | None = Field(default=None, max_length=500)  # official pricing URL
    embedding_pricing_verified_on: date | None = None
    embedding_pricing_is_placeholder: bool = True  # set false only after checking the official price

    # --- Distributed health ---
    health_backend: Literal["redis", "memory"] = "redis"
    health_key_ttl_seconds: int = Field(default=86_400, ge=60)
    health_probe_ttl_seconds: float = Field(default=30.0, gt=0, le=600)

    # --- Rate limiting ---
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = Field(default=60, ge=0)  # 0 = no per-minute limit
    rate_limit_per_hour: int = Field(default=1000, ge=0)  # 0 = no per-hour limit
    rate_limit_fail_open: bool = True  # Redis down -> allow (true) or 503 (false)
    # Per-model requests/minute across all instances, e.g. {"mock-advanced": 30}
    model_rate_limits: dict[str, int] = Field(default_factory=dict)
    # Only trust X-Forwarded-For when the direct peer is one of these proxies.
    trust_proxy_headers: bool = False
    trusted_proxies: list[str] = Field(default_factory=list)  # IPs or CIDRs

    # --- PostgreSQL persistence ---
    database_enabled: bool = True
    database_auto_migrate: bool = True
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_connect_timeout_seconds: int = Field(default=3, ge=1, le=60)
    # Phase 11.2 (H2): an unavailable database (at startup or after repeated write failures) is
    # reconnected in the background with exponential backoff: initial delay, doubling, capped.
    database_reconnect_initial_seconds: float = Field(default=1.0, gt=0, le=60)
    database_reconnect_max_seconds: float = Field(default=30.0, ge=1, le=600)
    # Connection-level write failures retried per batch before handing it to the reconnect loop.
    persistence_write_max_attempts: int = Field(default=3, ge=1, le=10)
    persist_prompts: bool = False  # store raw prompt text (off by default: sensitive)
    persist_responses: bool = False  # store raw answers (off by default: sensitive)
    persistence_queue_size: int = Field(default=1000, ge=1, le=100_000)

    # --- Admin ---
    # Required for DELETE /api/v1/cache. Unset -> admin endpoints are disabled.
    admin_api_token: SecretStr | None = None

    # =================== Phase 5 step 7: HTTP hardening ===================
    # Interactive API docs (/docs, /redoc, /openapi.json). None = enabled except in production.
    docs_enabled: bool | None = None
    # Exact origins allowed for browser CORS requests (JSON list). Empty = no CORS headers
    # at all (browsers block cross-origin calls). "*" is rejected in production.
    cors_allowed_origins: list[str] = Field(default_factory=list)
    # Hard cap on request body size, enforced while streaming (also without Content-Length).
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024, le=100 * 1_048_576)
    security_headers_enabled: bool = True
    # Strict-Transport-Security: enable ONLY when the API is served over HTTPS.
    hsts_enabled: bool = False
    hsts_max_age_seconds: int = Field(default=31_536_000, ge=0)

    # =================== Phase 5 step 8: chat authentication ===================
    # none    -> /api/v1/chat is open (development default; rejected in production)
    # api_key -> "Authorization: Bearer <key>" required; keys from CHAT_API_KEYS.
    chat_auth_mode: Literal["none", "api_key"] = "none"
    # JSON object {"<client-name>": "<key>"}. The NAME (never the key) identifies the
    # caller for rate limits, budgets, logs (hashed) and persistence. These keys are
    # separate from ADMIN_API_TOKEN and OBSERVABILITY_API_TOKEN (must all differ).
    chat_api_keys: dict[str, SecretStr] = Field(default_factory=dict)

    # =================== Phase 5 step 8: real providers + spend safeguards ===================
    # Real provider calls need BOTH LLM_MOCK_MODE=false AND ALLOW_REAL_PROVIDER_CALLS=true.
    # LLM_MOCK_MODE=false alone refuses to start (no accidental paid calls).
    allow_real_provider_calls: bool = False
    # Daily USD limits (UTC day), shared across instances via Redis. 0 = no limit.
    # BUDGET_DAILY_USD is REQUIRED (> 0) when real provider calls are enabled.
    budget_daily_usd: float = Field(default=0.0, ge=0, le=1_000_000)
    budget_daily_usd_per_client: float = Field(default=0.0, ge=0, le=1_000_000)
    budget_daily_tokens_per_client: int = Field(default=0, ge=0)
    # Reject a single provider attempt whose WORST-CASE cost exceeds this. 0 = no limit.
    budget_max_cost_per_request_usd: float = Field(default=0.0, ge=0, le=10_000)
    # Redis unreachable while budgets are configured: true = reject (503), false = allow.
    budget_fail_closed: bool = True
    # Upper bound for max_output_tokens sent to REAL providers (per attempt).
    real_max_output_tokens: int = Field(default=4096, ge=1, le=200_000)

    @property
    def real_provider_calls_enabled(self) -> bool:
        return (not self.llm_mock_mode) and self.allow_real_provider_calls

    @property
    def docs_effective(self) -> bool:
        return self.docs_enabled if self.docs_enabled is not None else self.environment != "production"

    @field_validator(
        "openai_api_key", "anthropic_api_key", "gemini_api_key", "groq_api_key", "mistral_api_key",
        "admin_api_token", "redis_password", "client_id_hash_key", "observability_api_token",
        "embedding_pricing_source",
        mode="before",
    )
    @classmethod
    def _blank_key_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("model_registry_file", mode="after")
    @classmethod
    def _resolve_registry_path(cls, v: Path | None) -> Path | None:
        if v is None or str(v).strip() in ("", "."):
            return None
        return v if v.is_absolute() else PROJECT_ROOT / v

    @field_validator("cache_cacheable_max_output_tokens", mode="after")
    @classmethod
    def _check_cacheable_tokens(cls, v: list[int]) -> list[int]:
        if len(v) > 64 or any(not 1 <= t <= 200_000 for t in v):
            raise ValueError("CACHE_CACHEABLE_MAX_OUTPUT_TOKENS: at most 64 values, each 1..200000")
        return sorted(set(v))

    @model_validator(mode="after")
    def _check_classifier_thresholds(self) -> "Settings":
        if self.classifier_simple_threshold >= self.classifier_complex_threshold:
            raise ValueError("CLASSIFIER_SIMPLE_THRESHOLD must be lower than CLASSIFIER_COMPLEX_THRESHOLD")
        return self

    @model_validator(mode="after")
    def _check_chat_auth(self) -> "Settings":
        """Invariants valid in every environment. Messages never contain key values."""
        for name, key in self.chat_api_keys.items():
            if not CHAT_CLIENT_NAME.fullmatch(name):
                raise ValueError("CHAT_API_KEYS: client names must match [A-Za-z0-9._-]{1,64}")
            if len(key.get_secret_value()) < MIN_CHAT_KEY_LENGTH:
                raise ValueError(f"CHAT_API_KEYS[{name}]: key must be at least {MIN_CHAT_KEY_LENGTH} characters")
        if self.chat_auth_mode == "api_key" and not self.chat_api_keys:
            raise ValueError("CHAT_AUTH_MODE=api_key requires at least one entry in CHAT_API_KEYS")
        values = [k.get_secret_value() for k in self.chat_api_keys.values()]
        if len(set(values)) != len(values):
            raise ValueError("CHAT_API_KEYS: every client must have a distinct key")
        others = {s.get_secret_value() for s in (self.admin_api_token, self.observability_api_token) if s is not None}
        if others & set(values):
            raise ValueError("CHAT_API_KEYS must not reuse ADMIN_API_TOKEN or OBSERVABILITY_API_TOKEN")
        return self

    def provider_api_keys(self) -> dict[str, SecretStr]:
        """Provider name -> key. Values stay SecretStr so they never print in logs."""
        keys = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "gemini": self.gemini_api_key,
            "groq": self.groq_api_key,
            "mistral": self.mistral_api_key,
        }
        return {name: key for name, key in keys.items() if key is not None}

    def secret_values(self) -> list[str]:
        """Every configured secret value, for log redaction. Never log this list."""
        candidates = [
            *self.provider_api_keys().values(),
            self.admin_api_token, self.redis_password, self.postgres_password, self.client_id_hash_key,
            self.observability_api_token, *self.chat_api_keys.values(),
        ]
        return [s.get_secret_value() for s in candidates if s is not None and s.get_secret_value()]

    # Connection URLs are plain properties (NOT computed fields), so they never
    # appear in model_dump()/JSON/repr. They contain the password: pass them only
    # to the client libraries, never to logs.
    @property
    def redis_url(self) -> str:
        auth = f":{quote(self.redis_password.get_secret_value(), safe='')}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def database_url(self) -> str:
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return f"postgresql+psycopg://{user}:{password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    @property
    def safe_database_url(self) -> str:
        """For logs/diagnostics: password masked."""
        return f"postgresql+psycopg://{self.postgres_user}:***@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    @property
    def safe_redis_url(self) -> str:
        auth = ":***@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (use as a FastAPI dependency)."""
    return Settings()
