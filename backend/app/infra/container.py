"""Composition root: builds every Phase 4 component from Settings.

One container per process (per FastAPI app): one Redis pool, one DB engine,
one recorder worker. Connections are opened in `start()` (FastAPI lifespan)
and closed in `stop()`.
"""

import logging
from dataclasses import dataclass

from app.budget.embedding import BudgetedEmbedder
from app.budget.guard import BudgetGuard, LocalBudgetStore, RedisBudgetStore
from app.providers.catalog import enforce_embedding_configuration
from app.cache.semantic import SemanticCache
from app.core.config import Settings
from app.core.logging import log_event
from app.db.recorder import NullRecorder, RequestRecorder
from app.embeddings.base import EmbeddingProvider
from app.embeddings.local import LocalHashingEmbedder
from app.embeddings.litellm_embedder import LiteLLMEmbedder
from app.infra.database import Database
from app.infra.redis import RedisManager
from app.observability.metrics import Metrics
from app.observability.tracing import Tracing
from app.routing.registry import load_registry
from app.ratelimit.limiter import ClientIdentityResolver, RateLimiter
from app.routing.factory import budget_limits, build_chat_service
from app.routing.health import HealthTracker
from app.routing.health_store import HealthStore, LocalHealthStore, RedisHealthStore
from app.routing.service import ChatService

logger = logging.getLogger("app.infra.container")


@dataclass
class Container:
    settings: Settings
    redis: RedisManager
    database: Database
    recorder: RequestRecorder | NullRecorder
    cache: SemanticCache | None
    rate_limiter: RateLimiter | None
    identity: ClientIdentityResolver
    health: HealthStore
    chat_service: ChatService
    metrics: Metrics
    tracing: Tracing

    async def start(self) -> None:
        await self.redis.start()
        await self.database.start()
        if isinstance(self.recorder, RequestRecorder):
            await self.recorder.start()
        log_event(
            logger, "infra.started", redis=self.redis.enabled, database=self.database.available,
            cache=self.cache is not None, rate_limit=self.rate_limiter is not None, health_backend=self.health.backend,
        )

    async def stop(self) -> None:
        if isinstance(self.recorder, RequestRecorder):
            await self.recorder.stop()
        await self.database.close()
        await self.redis.close()
        self.tracing.shutdown()  # flush/close exporters


def build_embedder(settings: Settings, budget: BudgetGuard | None = None) -> EmbeddingProvider:
    """Local hashing embedder (free, default) or a PAID LiteLLM embedder that is ALWAYS
    wrapped in budget enforcement (step 8A). A paid embedder without a budget is refused."""
    if settings.embedding_provider == "litellm":
        enforce_embedding_configuration(settings)
        if budget is None:  # unreachable after the gate (BUDGET_DAILY_USD > 0); never silently unbudgeted
            raise RuntimeError("paid embeddings require a budget guard")
        provider = settings.embedding_model.split("/", 1)[0] if "/" in settings.embedding_model else ""
        inner = LiteLLMEmbedder(
            settings.embedding_model,
            api_key=settings.provider_api_keys().get(provider),
            timeout_seconds=settings.embedding_timeout_seconds,
        )
        return BudgetedEmbedder(inner, budget, input_cost_per_million=settings.embedding_input_cost_per_million)
    return LocalHashingEmbedder(settings.embedding_dimensions)


def build_tracing(settings: Settings) -> Tracing:
    return Tracing(
        enabled=settings.tracing_enabled, exporter=settings.tracing_exporter,
        service_name=settings.tracing_service_name, service_version=settings.app_version,
        environment=settings.environment, sample_ratio=settings.tracing_sample_ratio,
        trust_incoming=settings.tracing_trust_incoming, otlp_endpoint=settings.tracing_otlp_endpoint,
    )


def build_container(settings: Settings, *, chat_service: ChatService | None = None) -> Container:
    enforce_embedding_configuration(settings)  # step 8A: refuse unsafe paid embeddings at startup
    tracing = build_tracing(settings)
    redis = RedisManager(
        settings.redis_url,
        enabled=settings.redis_enabled,
        key_prefix=settings.redis_key_prefix,
        connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        max_connections=settings.redis_max_connections,
        health_check_interval=settings.redis_health_check_interval_seconds,
    )
    database = Database(
        settings.database_url,
        enabled=settings.database_enabled,
        auto_migrate=settings.database_auto_migrate,
        pool_size=settings.database_pool_size,
        connect_timeout=settings.database_connect_timeout_seconds,
        reconnect_initial_seconds=settings.database_reconnect_initial_seconds,
        reconnect_max_seconds=settings.database_reconnect_max_seconds,
    )
    recorder: RequestRecorder | NullRecorder = (
        RequestRecorder(
            database, queue_size=settings.persistence_queue_size,
            persist_prompts=settings.persist_prompts, persist_responses=settings.persist_responses,
            tracer=tracing.tracer, write_max_attempts=settings.persistence_write_max_attempts,
        )
        if settings.database_enabled
        else NullRecorder()
    )

    use_redis = settings.redis_enabled
    # One guard for every paid operation (chat attempts AND paid embeddings, step 8A).
    limits = budget_limits(settings)
    budget = (
        BudgetGuard(RedisBudgetStore(redis) if use_redis else LocalBudgetStore(), limits,
                    fail_closed=settings.budget_fail_closed)
        if limits.configured else None
    )

    health: HealthStore = (
        RedisHealthStore(
            redis,
            failure_threshold=settings.health_failure_threshold,
            cooldown_seconds=settings.health_cooldown_seconds,
            key_ttl_seconds=settings.health_key_ttl_seconds,
            probe_ttl_seconds=settings.health_probe_ttl_seconds,
        )
        if use_redis and settings.health_backend == "redis"
        else LocalHealthStore(
            HealthTracker(failure_threshold=settings.health_failure_threshold,
                          cooldown_seconds=settings.health_cooldown_seconds)
        )
    )
    cache = (
        SemanticCache(
            redis,
            build_embedder(settings, budget),
            ttl_seconds=settings.cache_ttl_seconds,
            similarity_threshold=settings.cache_similarity_threshold,
            semantic_enabled=settings.cache_semantic_enabled,
            allow_lexical=settings.cache_semantic_allow_lexical,
            max_entries_per_scope=settings.cache_max_entries_per_scope,
            max_entries=settings.cache_max_entries,
            max_entry_bytes=settings.cache_max_entry_bytes,
            cacheable_max_output_tokens=frozenset(settings.cache_cacheable_max_output_tokens)
            | {settings.default_max_output_tokens},
            lock_timeout_seconds=settings.cache_lock_timeout_seconds,
            lock_wait_seconds=settings.cache_lock_wait_seconds,
            tracer=tracing.tracer,
        )
        if use_redis and settings.cache_enabled
        else None
    )
    if cache is not None and cache.semantic_gated:
        log_event(logger, "cache.semantic_gated", logging.WARNING, embedder=cache.embedder.id,
                  reason="lexical embedder cannot guarantee equivalence; exact-match caching only")
    elif cache is not None and cache.semantic_enabled and not cache.embedder.is_semantic:
        log_event(logger, "cache.semantic_lexical_unsafe", logging.WARNING, embedder=cache.embedder.id,
                  reason="CACHE_SEMANTIC_ALLOW_LEXICAL=true: entity/word-order changes may hit")
    rate_limiter = (
        RateLimiter(
            redis,
            per_minute=settings.rate_limit_per_minute,
            per_hour=settings.rate_limit_per_hour,
            model_limits=settings.model_rate_limits,
            fail_open=settings.rate_limit_fail_open,
        )
        if use_redis and settings.rate_limit_enabled
        else None
    )

    if chat_service is None:
        registry = load_registry(settings.model_registry_file)
        models = registry.all()
        # One isolated Prometheus registry per app instance (no global state).
        metrics = Metrics(
            enabled=settings.metrics_enabled, model_ids=[m.id for m in models],
            providers={m.provider for m in models}, app_version=settings.app_version,
            mock_mode=settings.llm_mock_mode,
        )
        service = build_chat_service(
            settings, registry=registry, health=health, cache=cache, recorder=recorder,
            model_limiter=rate_limiter if (rate_limiter and settings.model_rate_limits) else None,
            metrics=metrics, tracer=tracing.tracer, budget=budget,
        )
    else:
        service, metrics = chat_service, chat_service.metrics
    if cache is not None and isinstance(cache.embedder, BudgetedEmbedder):
        cache.embedder.metrics = metrics  # bounded embedding budget metrics
    if cache is not None:
        cache.metrics = metrics  # llm_router_cache_degraded_total
    database.metrics = metrics  # llm_router_db_available / _reconnect_attempts_total (Phase 11.2)
    if isinstance(recorder, RequestRecorder):
        recorder.metrics = metrics  # llm_router_db_persist_records_total / _write_retries_total
    return Container(
        settings=settings, redis=redis, database=database, recorder=recorder, cache=cache,
        rate_limiter=rate_limiter, identity=ClientIdentityResolver(
            trust_proxy_headers=settings.trust_proxy_headers, trusted_proxies=settings.trusted_proxies),
        health=service.health, chat_service=service, metrics=metrics, tracing=tracing,
    )
