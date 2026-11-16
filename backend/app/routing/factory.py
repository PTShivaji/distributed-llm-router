"""Builds a fully wired ChatService from Settings (composition root)."""

from app.budget.guard import BudgetGuard, BudgetLimits, LocalBudgetStore
from app.classifier import QueryClassifier, Thresholds
from app.core.config import Settings
from app.providers.catalog import enforce_provider_configuration
from app.providers.litellm_provider import CompletionFn, LiteLLMProvider
from app.providers.mock import MockProvider
from app.providers.pool import ProviderPool
from typing import TYPE_CHECKING

from app.routing.health import HealthTracker
from app.routing.health_store import HealthStore, LocalHealthStore

if TYPE_CHECKING:
    from app.cache.semantic import SemanticCache
    from app.db.recorder import Recorder
    from opentelemetry.trace import Tracer

    from app.observability.metrics import Metrics
    from app.ratelimit.limiter import RateLimiter
from app.routing.policy import RoutingPolicy
from app.routing.registry import ModelRegistry, load_registry
from app.routing.service import ChatService, ExecutionSettings


def budget_limits(settings: Settings) -> BudgetLimits:
    return BudgetLimits(
        daily_usd=settings.budget_daily_usd,
        daily_usd_per_client=settings.budget_daily_usd_per_client,
        daily_tokens_per_client=settings.budget_daily_tokens_per_client,
        max_cost_per_request_usd=settings.budget_max_cost_per_request_usd,
    )


def build_chat_service(
    settings: Settings,
    *,
    registry: ModelRegistry | None = None,
    health: "HealthStore | None" = None,
    cache: "SemanticCache | None" = None,
    recorder: "Recorder | None" = None,
    model_limiter: "RateLimiter | None" = None,
    metrics: "Metrics | None" = None,
    tracer: "Tracer | None" = None,
    budget: BudgetGuard | None = None,
    completion_fn: CompletionFn | None = None,
) -> ChatService:
    """Phase 3 defaults (process-local health, no cache, no persistence) unless
    Phase 4 components are passed in by the container.

    `completion_fn` replaces litellm.acompletion (dependency injection for tests:
    a fake client means no network and no paid call). Real provider calls are only
    wired when LLM_MOCK_MODE=false AND ALLOW_REAL_PROVIDER_CALLS=true, after the
    startup safety check (app/providers/catalog.py) has passed.
    """
    registry = registry or load_registry(settings.model_registry_file)
    enforce_provider_configuration(settings, registry)
    tracker = health.tracker if isinstance(health, LocalHealthStore) else HealthTracker(
        failure_threshold=settings.health_failure_threshold, cooldown_seconds=settings.health_cooldown_seconds
    )
    store = health or LocalHealthStore(tracker)
    api_keys = settings.provider_api_keys()
    providers = ProviderPool(
        mock=MockProvider(settings.mock_failures, latency_ms=settings.mock_latency_ms),
        mock_mode=settings.llm_mock_mode,
        real_factory=(lambda: LiteLLMProvider(api_keys, completion_fn=completion_fn))
        if settings.real_provider_calls_enabled else None,
    )
    limits = budget_limits(settings)
    if budget is None and limits.configured:
        budget = BudgetGuard(LocalBudgetStore(), limits, fail_closed=settings.budget_fail_closed)
    policy = RoutingPolicy(
        registry,
        tracker,  # default only; ChatService passes a per-request view from `store`
        strategy=settings.routing_strategy,
        tier_escalation=settings.routing_tier_escalation,
        fallback_auto_tier=settings.fallback_auto_tier,
        max_fallback_models=settings.max_fallback_models,
    )
    classifier = QueryClassifier(
        Thresholds(simple_max=settings.classifier_simple_threshold, complex_min=settings.classifier_complex_threshold)
    )
    execution = ExecutionSettings(
        default_timeout_seconds=settings.provider_timeout_seconds,
        max_retries=settings.provider_max_retries,
        retry_backoff_seconds=settings.provider_retry_backoff_seconds,
        default_max_output_tokens=settings.default_max_output_tokens,
        fallback_enabled=settings.fallback_enabled,
        fallback_on_override=settings.fallback_on_override,
        estimate_missing_usage=settings.estimate_missing_usage,
        real_max_output_tokens=settings.real_max_output_tokens,
    )
    return ChatService(
        classifier=classifier, policy=policy, providers=providers, health=store, execution=execution,
        cache=cache, recorder=recorder, model_limiter=model_limiter, metrics=metrics, tracer=tracer,
        budget=budget,
    )
