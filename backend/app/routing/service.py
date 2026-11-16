"""ChatService: classify -> route -> cache -> execute (retry + fallback) -> usage/cost -> persist.

Every decision is logged as a structured event and returned in the result,
so a response can be traced end to end by its request id. Raw user text and
secrets are never logged (only sizes, ids and error kinds).

Phase 4 additions are all optional collaborators with Phase 3 defaults:
  health      HealthStore (Redis, shared) | HealthTracker (process-local)
  cache       SemanticCache | None
  recorder    RequestRecorder | NullRecorder
  model_limiter RateLimiter | None (per-model request limits)
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.budget.guard import NOT_BILLED, BudgetGuard, Reservation, request_budget
from app.cache.semantic import CacheLookup, CacheStatus, SemanticCache, cache_scope
from app.classifier import ClassificationResult, QueryClassifier
from app.core.logging import log_event
from app.db.recorder import NullRecorder, Recorder, RequestRecord
from app.observability.metrics import Metrics
from app.observability.tracing import NOOP_TRACER, current_span_context, mark_error, set_attributes, start_span
from opentelemetry.trace import Tracer
from app.providers.base import ProviderError, ProviderErrorKind, ProviderRequest, ProviderResponse
from app.providers.pool import ProviderPool
from app.ratelimit.limiter import RateLimiter, hash_subject
from app.routing.errors import AllModelsFailedError, BudgetExceededError, BudgetUnavailableError
from app.routing.health import HealthState, HealthTracker
from app.routing.health_store import HealthStore, HealthView, LocalHealthStore
from app.routing.policy import RoutingDecision, RoutingPolicy
from app.routing.registry import ModelConfig
from app.routing.usage import (
    CostEstimate, TokenUsage, build_usage, estimate_cost, estimate_tokens, upper_bound_input_tokens,
)

logger = logging.getLogger("app.routing")

Outcome = Literal["success", "error", "skipped_unhealthy", "skipped_rate_limited", "skipped_budget"]


@dataclass(frozen=True, slots=True)
class ChatCommand:
    query: str
    request_id: str
    model_override: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    client_id: str | None = None
    use_cache: bool = True


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_number: int
    model_id: str
    provider: str
    outcome: Outcome
    error_kind: str | None
    retryable: bool | None
    latency_ms: float


@dataclass(frozen=True, slots=True)
class FallbackInfo:
    used: bool
    original_model: str
    final_model: str
    chain: tuple[str, ...]
    attempts: tuple[AttemptRecord, ...]
    reason: str | None


@dataclass(frozen=True, slots=True)
class CacheInfo:
    status: CacheStatus
    similarity: float | None = None
    threshold: float | None = None
    embedding_provider: str | None = None
    age_seconds: float | None = None
    source_request_id: str | None = None
    saved_tokens: int | None = None
    saved_cost: float | None = None
    stored: bool = False
    lookup_ms: float | None = None


@dataclass(frozen=True, slots=True)
class ChatOutcome:
    request_id: str
    answer: str
    classification: ClassificationResult
    routing: RoutingDecision
    selected_model: str
    model_used: str
    provider: str
    provider_adapter: str
    provider_model: str
    finish_reason: str | None
    fallback: FallbackInfo
    usage: TokenUsage
    cost: CostEstimate
    latency_ms: float
    provider_status: HealthState
    mock_mode: bool
    cache: CacheInfo = CacheInfo(status="disabled")


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    default_timeout_seconds: float = 30.0
    max_retries: int = 1
    retry_backoff_seconds: float = 0.25
    default_max_output_tokens: int = 1024
    fallback_enabled: bool = True
    fallback_on_override: bool = False
    estimate_missing_usage: bool = True
    # Cap for max_output_tokens on REAL (non-mock) provider calls; None = no extra cap.
    real_max_output_tokens: int | None = None


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


class ChatService:
    def __init__(
        self,
        *,
        classifier: QueryClassifier,
        policy: RoutingPolicy,
        providers: ProviderPool,
        health: HealthStore | HealthTracker,
        execution: ExecutionSettings,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        cache: SemanticCache | None = None,
        recorder: Recorder | None = None,
        model_limiter: RateLimiter | None = None,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        budget: BudgetGuard | None = None,
    ) -> None:
        self.tracer: Tracer = tracer or NOOP_TRACER
        # Spend safeguards: consulted only for REAL provider attempts (mock calls are free).
        self.budget = budget
        self.classifier = classifier
        self.policy = policy
        self.providers = providers
        self.health: HealthStore = LocalHealthStore(health) if isinstance(health, HealthTracker) else health
        self.execution = execution
        self.cache = cache
        self.recorder: Recorder = recorder or NullRecorder()
        self.model_limiter = model_limiter
        models = policy.registry.all()
        # Default: a private registry (Phase 3 tests / standalone use stay isolated).
        self.metrics = metrics or Metrics(
            model_ids=[m.id for m in models], providers={m.provider for m in models}, mock_mode=providers.mock_mode
        )
        self._sleep = sleep

    # ------------------------------------------------------------------ #
    async def chat(self, cmd: ChatCommand) -> ChatOutcome:
        with start_span(self.tracer, "chat", **{
            "chat.query_chars": len(cmd.query), "chat.use_cache": cmd.use_cache,
            "chat.override": cmd.model_override is not None, "request_id": cmd.request_id,
        }) as span, request_budget(cmd.client_id):
            # One spend scope per request: the per-request limit covers embeddings AND every
            # chat attempt (retries + fallbacks) of this request (step 8A).
            outcome = await self._chat(cmd)
            set_attributes(span, **{
                "chat.outcome": "success", "chat.selected_model": outcome.selected_model,
                "chat.model_used": outcome.model_used, "chat.fallback_used": outcome.fallback.used,
                "cache.status": outcome.cache.status, "chat.attempts": len(outcome.fallback.attempts),
            })
            return outcome

    async def _chat(self, cmd: ChatCommand) -> ChatOutcome:
        started = time.perf_counter()
        log_event(
            logger, "chat.request_received", query_chars=len(cmd.query), override=cmd.model_override,
            temperature=cmd.temperature, max_output_tokens=cmd.max_output_tokens, use_cache=cmd.use_cache,
        )

        with start_span(self.tracer, "classify") as span:
            classification = self.classifier.classify(cmd.query)
            set_attributes(span, **{
                "chat.complexity": classification.complexity, "chat.intent": classification.intent.primary,
                "chat.complexity_score": classification.complexity_score,
                "chat.confidence": classification.confidence, "chat.is_coding": classification.technical.is_coding,
            })
        log_event(
            logger, "chat.classified", complexity=classification.complexity, score=classification.complexity_score,
            confidence=classification.confidence, intent=classification.intent.primary,
            coding=classification.technical.is_coding,
        )

        enabled_ids = [m.id for m in self.policy.registry.enabled()]
        with start_span(self.tracer, "health.view", **{"health.backend": self.health.backend,
                                                       "health.models": len(enabled_ids)}):
            view = await self.health.view(enabled_ids)
        est_input = estimate_tokens(cmd.query)
        max_out = cmd.max_output_tokens or self.execution.default_max_output_tokens
        with start_span(self.tracer, "route.select") as span:
            decision = self.policy.select(
                classification, override=cmd.model_override, estimated_input_tokens=est_input,
                max_output_tokens=max_out, health=view,
            )
            set_attributes(span, **{
                "route.selected_model": decision.model.id, "route.requested_tier": decision.requested_tier,
                "route.routed_tier": decision.routed_tier, "route.strategy": decision.strategy,
                "route.override": decision.override, "route.candidates": len(decision.candidates),
            })
        log_event(
            logger, "chat.model_selected", model=decision.model.id, requested_tier=decision.requested_tier,
            routed_tier=decision.routed_tier, strategy=decision.strategy, override=decision.override,
        )

        if self.cache is not None and cmd.use_cache and self._cacheable(cmd, decision, max_out):
            with start_span(self.tracer, "cache.lookup", **{"cache.semantic_enabled": self.cache.semantic_enabled}) as span:
                lookup = await self._cache_lookup(cmd, decision, max_out)
                set_attributes(span, **{"cache.status": lookup.status if lookup else None,
                                        "cache.similarity": lookup.similarity if lookup else None,
                                        "cache.lock_acquired": bool(lookup and lookup.lock_token)})
        else:
            lookup = None
        cache_status = lookup.status if lookup is not None else self._cache_disabled_status(cmd)
        self.metrics.cache_lookup(cache_status)
        try:
            if lookup is not None and lookup.hit and lookup.payload is not None:
                outcome = self._outcome_from_cache(cmd, classification, decision, lookup, view, started)
            else:
                try:
                    outcome = await self._execute(cmd, classification, decision, view, lookup, max_out, est_input, started)
                except (AllModelsFailedError, BudgetExceededError, BudgetUnavailableError):
                    # Budget refusals are failed chat requests too (step 9: they were not counted).
                    self.metrics.chat_completed(complexity=classification.complexity, selected_model=decision.model.id,
                                                model_used=None, cache_status=cache_status, outcome="error")
                    raise
            self._observe(outcome)
            self._record(cmd, outcome=outcome)
            return outcome
        finally:
            if lookup is not None and self.cache is not None:
                await self.cache.release(lookup)

    def _observe(self, outcome: ChatOutcome) -> None:
        self.metrics.chat_completed(
            complexity=outcome.classification.complexity, selected_model=outcome.selected_model,
            model_used=outcome.model_used, cache_status=outcome.cache.status, outcome="success",
        )
        u, c = outcome.usage, outcome.cost
        self.metrics.usage(outcome.model_used, u.input_tokens, u.output_tokens, u.source, c.total_cost,
                           c.pricing_is_placeholder)

    # ------------------------------------------------------------------ #
    async def _execute(
        self,
        cmd: ChatCommand,
        classification: ClassificationResult,
        decision: RoutingDecision,
        view: HealthView,
        lookup: CacheLookup | None,
        max_out: int,
        est_input: int,
        started: float,
    ) -> ChatOutcome:
        chain = [decision.model]
        if self.execution.fallback_enabled and (not decision.override or self.execution.fallback_on_override):
            chain += self.policy.fallback_chain(
                decision.model, classification, estimated_input_tokens=est_input, max_output_tokens=max_out
            )

        attempts: list[AttemptRecord] = []
        with start_span(self.tracer, "provider.chain", **{"route.selected_model": decision.model.id,
                                                          "route.fallback_chain_length": len(chain) - 1}) as span:
            post_state: dict[str, HealthState] = {}  # per request: state returned by the success update
            response, used, last_error = await self._run_chain(chain, cmd, max_out, attempts, view, post_state)
            set_attributes(span, **{"chat.attempts": len(attempts), "chat.model_used": used.id if used else None,
                                    "chat.fallback_used": bool(used and used.id != decision.model.id)})
            if response is None:
                mark_error(span, "all_models_failed", **{"llm.error_kind": last_error.kind if last_error else None})

        if response is None or used is None:
            latency = _ms(started)
            budget_error = self._budget_error(attempts)
            log_event(
                logger, "chat.all_models_failed", logging.ERROR, attempts=len(attempts),
                last_error=last_error.kind if last_error else None, latency_ms=latency,
            )
            error = AllModelsFailedError(
                "All candidate models failed to produce a response.",
                details={
                    "selected_model": decision.model.id,
                    "attempts": [
                        {"model": a.model_id, "outcome": a.outcome, "error_kind": a.error_kind} for a in attempts
                    ],
                },
                rejected=bool(last_error and last_error.kind == ProviderErrorKind.CONTENT_POLICY),
            ) if budget_error is None else budget_error
            self._record(cmd, error=error, classification=classification, decision=decision, attempts=attempts,
                         cache_status=lookup.status if lookup else self._cache_disabled_status(cmd), latency=latency)
            raise error

        usage = build_usage(
            reported_input=response.input_tokens, reported_output=response.output_tokens,
            prompt_text=cmd.query, completion_text=response.content,
            estimate_missing=self.execution.estimate_missing_usage,
        )
        cost = estimate_cost(usage, used)
        fallback_used = used.id != decision.model.id

        cache_info = CacheInfo(status=self._cache_disabled_status(cmd))
        if lookup is not None and self.cache is not None:
            # Only cache answers from the selected model: a fallback answer is not
            # stored under the primary model's scope.
            stored = False
            if not fallback_used and lookup.status != "error":
                with start_span(self.tracer, "cache.store") as span:
                    stored = await self.cache.store(lookup, cmd.query, self._cache_payload(cmd, decision, used, response, usage, cost))
                    set_attributes(span, **{"cache.stored": stored})
            cache_info = CacheInfo(
                status=lookup.status, similarity=lookup.similarity, threshold=self.cache.similarity_threshold,
                embedding_provider=self.cache.embedder.id if self.cache.semantic_enabled else None,
                stored=stored, lookup_ms=lookup.latency_ms,
            )

        latency = _ms(started)
        log_event(
            logger, "chat.completed", selected_model=decision.model.id, model_used=used.id,
            fallback_used=fallback_used, attempts=len(attempts), input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens, usage_source=usage.source, total_cost=cost.total_cost,
            cache_status=cache_info.status, latency_ms=latency,
        )
        # State returned by the atomic success update (saves a Redis round trip; step 6B).
        provider_status = post_state.get(used.id)
        if provider_status is None:  # defensive: should not happen after a success
            provider_status = (await self.health.view([used.id])).state(used.id)
        return ChatOutcome(
            request_id=cmd.request_id,
            answer=response.content,
            classification=classification,
            routing=decision,
            selected_model=decision.model.id,
            model_used=used.id,
            provider=used.provider,
            provider_adapter=self.providers.for_model(used).name,
            provider_model=response.provider_model,
            finish_reason=response.finish_reason,
            fallback=FallbackInfo(
                used=fallback_used,
                original_model=decision.model.id,
                final_model=used.id,
                chain=tuple(m.id for m in chain),
                attempts=tuple(attempts),
                reason=self._fallback_reason(attempts, decision.model.id) if fallback_used else None,
            ),
            usage=usage,
            cost=cost,
            latency_ms=latency,
            provider_status=provider_status,
            mock_mode=self.providers.mock_mode,
            cache=cache_info,
        )

    # ------------------------------------------------------------------ #
    def _cache_disabled_status(self, cmd: ChatCommand) -> CacheStatus:
        # Cache configured but not consulted: use_cache=false, or uncacheable parameters (Phase 11.1).
        return "bypass" if self.cache is not None else "disabled"

    def _cacheable(self, cmd: ChatCommand, decision: RoutingDecision, max_out: int) -> bool:
        """Phase 11.1 (H1): bounded cache-scope cardinality. Off-grid temperatures and non-allow-listed
        token limits are answered normally (exact parameters) but neither read nor write the cache."""
        assert self.cache is not None
        model = decision.model
        effective = min(max_out, model.max_output_tokens)  # the value the cache scope uses
        if self.cache.cacheable_parameters(temperature=cmd.temperature, max_output_tokens=effective,
                                           always_cacheable_tokens=(model.max_output_tokens,)):
            return True
        log_event(logger, "cache.bypass", reason="uncacheable_parameters", temperature=cmd.temperature,
                  max_output_tokens=effective)
        return False

    async def _cache_lookup(self, cmd: ChatCommand, decision: RoutingDecision, max_out: int) -> CacheLookup | None:
        if self.cache is None or not cmd.use_cache:
            return None
        model = decision.model
        scope = cache_scope(
            model_id=model.id, provider=model.provider, provider_model=model.provider_model,
            temperature=cmd.temperature, max_output_tokens=min(max_out, model.max_output_tokens),
            mock_mode=self.providers.mock_mode, embedding_id=self.cache.embedder.id,
        )
        return await self.cache.lookup(scope, cmd.query)

    @staticmethod
    def _cache_payload(
        cmd: ChatCommand, decision: RoutingDecision, used: ModelConfig, response: ProviderResponse,
        usage: TokenUsage, cost: CostEstimate,
    ) -> dict[str, Any]:
        """What a cache entry contains. Deliberately excludes the prompt text."""
        return {
            "answer": response.content,
            "model_used": used.id,
            "provider": used.provider,
            "provider_model": response.provider_model,
            "finish_reason": response.finish_reason,
            "complexity": decision.requested_tier,
            "source_request_id": cmd.request_id,
            "usage": asdict(usage),
            "total_cost": cost.total_cost,
        }

    def _outcome_from_cache(
        self,
        cmd: ChatCommand,
        classification: ClassificationResult,
        decision: RoutingDecision,
        lookup: CacheLookup,
        view: HealthView,
        started: float,
    ) -> ChatOutcome:
        """A cache hit: no provider call, 0 tokens and 0 cost for THIS request."""
        assert self.cache is not None and lookup.payload is not None
        p = lookup.payload
        used = self.policy.registry.get(p["model_used"]) or decision.model
        original_usage = p.get("usage") or {}
        latency = _ms(started)
        log_event(
            logger, "chat.completed", selected_model=decision.model.id, model_used=used.id, fallback_used=False,
            attempts=0, cache_status=lookup.status, similarity=lookup.similarity, latency_ms=latency,
        )
        return ChatOutcome(
            request_id=cmd.request_id,
            answer=p["answer"],
            classification=classification,
            routing=decision,
            selected_model=decision.model.id,
            model_used=used.id,
            provider=p.get("provider", used.provider),
            provider_adapter="cache",
            provider_model=p.get("provider_model", used.provider_model),
            finish_reason=p.get("finish_reason"),
            fallback=FallbackInfo(False, decision.model.id, used.id, (), (), None),
            usage=TokenUsage(0, 0, 0, "cache_hit"),
            cost=estimate_cost(TokenUsage(0, 0, 0, "cache_hit"), used),
            latency_ms=latency,
            provider_status=view.state(used.id),
            mock_mode=self.providers.mock_mode,
            cache=CacheInfo(
                status=lookup.status,
                similarity=lookup.similarity,
                threshold=self.cache.similarity_threshold,
                embedding_provider=self.cache.embedder.id if lookup.status == "hit_semantic" else None,
                age_seconds=round(time.time() - p["cached_at"], 3) if "cached_at" in p else None,
                source_request_id=p.get("source_request_id"),
                saved_tokens=original_usage.get("total_tokens"),
                saved_cost=p.get("total_cost"),
                lookup_ms=lookup.latency_ms,
            ),
        )

    # ------------------------------------------------------------------ #
    def _record(
        self,
        cmd: ChatCommand,
        *,
        outcome: ChatOutcome | None = None,
        error: "AllModelsFailedError | BudgetExceededError | BudgetUnavailableError | None" = None,
        classification: ClassificationResult | None = None,
        decision: RoutingDecision | None = None,
        attempts: list[AttemptRecord] | None = None,
        cache_status: str = "disabled",
        latency: float = 0.0,
    ) -> None:
        """Enqueue a persistence record (non-blocking). Raw text only if explicitly enabled."""
        c = outcome.classification if outcome else classification
        d = outcome.routing if outcome else decision
        rec = RequestRecord(
            request_id=cmd.request_id,
            status="success" if outcome else "error",
            error_code=error.code if error else None,
            query_chars=len(cmd.query),
            latency_ms=outcome.latency_ms if outcome else latency,
            client_hash=hash_subject(cmd.client_id) if cmd.client_id else None,
            prompt_text=cmd.query if self.recorder.persist_prompts else None,
            response_text=outcome.answer if (outcome and self.recorder.persist_responses) else None,
            complexity=c.complexity if c else None,
            complexity_score=c.complexity_score if c else None,
            classification_confidence=c.confidence if c else None,
            intent=c.intent.primary if c else None,
            is_coding=c.technical.is_coding if c else None,
            selected_model=d.model.id if d else None,
            model_used=outcome.model_used if outcome else None,
            provider=outcome.provider if outcome else None,
            override=d.override if d else False,
            fallback_used=outcome.fallback.used if outcome else False,
            mock_mode=self.providers.mock_mode,
            cache_status=outcome.cache.status if outcome else cache_status,
            cache_similarity=outcome.cache.similarity if outcome else None,
            attempts=[
                {"attempt_number": a.attempt_number, "model_id": a.model_id, "provider": a.provider,
                 "outcome": a.outcome, "error_kind": a.error_kind, "retryable": a.retryable,
                 "latency_ms": a.latency_ms}
                for a in (outcome.fallback.attempts if outcome else attempts or [])
            ],
        )
        if outcome:
            u, k = outcome.usage, outcome.cost
            rec.input_tokens, rec.output_tokens, rec.total_tokens, rec.usage_source = (
                u.input_tokens, u.output_tokens, u.total_tokens, u.source)
            rec.input_cost, rec.output_cost, rec.total_cost, rec.pricing_is_placeholder = (
                k.input_cost, k.output_cost, k.total_cost, k.pricing_is_placeholder)
        rec.span_context = current_span_context()  # link target for the background persist span
        self.recorder.record(rec)

    # ------------------------------------------------------------------ #
    async def _run_chain(
        self,
        chain: list[ModelConfig],
        cmd: ChatCommand,
        max_out: int,
        attempts: list[AttemptRecord],
        view: HealthView,
        post_state: dict[str, HealthState] | None = None,
    ) -> tuple[ProviderResponse | None, ModelConfig | None, ProviderError | None]:
        last_error: ProviderError | None = None
        for index, model in enumerate(chain):  # chain is finite + de-duplicated -> no loops
            if index > 0:
                if not view.is_available(model.id):
                    attempts.append(AttemptRecord(len(attempts) + 1, model.id, model.provider, "skipped_unhealthy", None, None, 0.0))
                    self.metrics.provider_attempt(model.id, model.provider, "skipped_unhealthy", None, None)
                    log_event(logger, "chat.fallback_skipped_unhealthy", logging.WARNING, model=model.id)
                    continue
                log_event(
                    logger, "chat.fallback_attempt", logging.WARNING, from_model=chain[0].id, to_model=model.id,
                    previous_error=last_error.kind if last_error else None,
                )

            # Recovery probe: a half-open model may only be called by the instance holding the probe lock.
            if view.needs_probe(model.id) and not await self.health.acquire_probe(model.id):
                attempts.append(AttemptRecord(len(attempts) + 1, model.id, model.provider, "skipped_unhealthy", None, None, 0.0))
                self.metrics.provider_attempt(model.id, model.provider, "skipped_unhealthy", None, None)
                log_event(logger, "chat.probe_in_progress_elsewhere", logging.WARNING, model=model.id)
                continue

            if self.model_limiter is not None:
                with start_span(self.tracer, "ratelimit.check", **{"ratelimit.scope": "model", "llm.model": model.id,
                                                                   "db.system": "redis"}) as span:
                    limit = await self.model_limiter.check_model(model.id)
                    set_attributes(span, **{"ratelimit.allowed": limit.allowed, "ratelimit.window": limit.window,
                                            "ratelimit.degraded": limit.degraded})
                if not limit.allowed:
                    attempts.append(AttemptRecord(len(attempts) + 1, model.id, model.provider, "skipped_rate_limited", "rate_limit", None, 0.0))
                    self.metrics.provider_attempt(model.id, model.provider, "skipped_rate_limited", "rate_limit", None)
                    self.metrics.rate_limit_rejected("model", limit.window)
                    log_event(logger, "ratelimit.model_rejected", logging.WARNING, model=model.id,
                              limit=limit.limit, reset_seconds=limit.reset_seconds)
                    continue

            if index > 0:
                self.metrics.fallback(chain[0].id, model.id)
            response, last_error = await self._try_model(model, cmd, max_out, attempts, is_fallback=index > 0,
                                                         post_state=post_state)
            if response is not None:
                return response, model, None
            if last_error is not None and not last_error.fallback_allowed:
                log_event(logger, "chat.fallback_blocked", logging.WARNING, model=model.id, error_kind=last_error.kind)
                break
        return None, None, last_error

    @staticmethod
    def _budget_error(attempts: list[AttemptRecord]) -> "BudgetExceededError | BudgetUnavailableError | None":
        """Spend safeguards stopped every model that was actually attempted -> 429/503, not 503 'failed'."""
        tried = [a for a in attempts if a.outcome in ("success", "error", "skipped_budget")]
        if not tried or any(a.outcome != "skipped_budget" for a in tried):
            return None
        reasons = list(dict.fromkeys(a.error_kind for a in tried if a.error_kind))
        details = {"reasons": reasons, "attempts": [{"model": a.model_id, "reason": a.error_kind} for a in tried]}
        if reasons == ["budget_unavailable"]:
            return BudgetUnavailableError("Spend limits cannot be checked right now (budget store unreachable); "
                                          "request rejected (BUDGET_FAIL_CLOSED=true).", details=details)
        return BudgetExceededError("Spend limit reached for this request; no provider was called.", details=details)

    async def _record_health(self, model_id: str, success: bool, kind: str | None = None) -> HealthState | None:
        with start_span(self.tracer, "health.record", **{"health.backend": self.health.backend,
                                                         "health.model": model_id, "llm.outcome": "success" if success else "error"}):
            if success:
                return await self.health.record_success(model_id)
            else:
                await self.health.record_failure(model_id, kind or "unknown")

    async def _try_model(
        self, model: ModelConfig, cmd: ChatCommand, max_out: int, attempts: list[AttemptRecord],
        *, is_fallback: bool = False, post_state: dict[str, HealthState] | None = None,
    ) -> tuple[ProviderResponse | None, ProviderError | None]:
        provider = self.providers.for_model(model)
        is_real = provider.name != "mock"
        timeout = model.timeout_seconds or self.execution.default_timeout_seconds
        out_tokens = min(max_out, model.max_output_tokens)
        if is_real and self.execution.real_max_output_tokens:
            out_tokens = min(out_tokens, self.execution.real_max_output_tokens)
        request = ProviderRequest(
            model_id=model.id,
            provider=model.provider,
            provider_model=model.provider_model,
            messages=({"role": "user", "content": cmd.query},),
            max_output_tokens=out_tokens,
            timeout_seconds=timeout,
            temperature=cmd.temperature,
            api_base=model.api_base,
        )

        error: ProviderError | None = None
        for retry in range(self.execution.max_retries + 1):
            if retry:
                delay = self.execution.retry_backoff_seconds * (2 ** (retry - 1))
                log_event(logger, "chat.retry", logging.WARNING, model=model.id, retry=retry, delay_s=delay)
                self.metrics.retry(model.id)
                await self._sleep(delay)

            reservation: Reservation | None = None
            if is_real and self.budget is not None:
                decision = await self.budget.reserve(model, cmd.client_id, upper_bound_input_tokens(cmd.query),
                                                     out_tokens)
                if not decision.allowed:
                    attempts.append(AttemptRecord(len(attempts) + 1, model.id, model.provider, "skipped_budget",
                                                  decision.reason, None, 0.0))
                    self.metrics.provider_attempt(model.id, model.provider, "skipped_budget", "budget", None)
                    self.metrics.budget_rejected(decision.reason)
                    return None, error  # keep the previous provider error (if any) for fallback decisions
                reservation = decision.reservation

            started = time.perf_counter()
            log_event(
                logger, "provider.invoke", model=model.id, provider=model.provider, adapter=provider.name,
                attempt=len(attempts) + 1, timeout_s=timeout,
            )
            with start_span(self.tracer, "provider.attempt", **{
                "llm.model": model.id, "llm.provider": model.provider, "llm.adapter": provider.name,
                "llm.attempt": len(attempts) + 1, "llm.retry": retry, "llm.is_fallback": is_fallback,
                "llm.timeout_s": timeout,
            }) as span:
                try:
                    response = await asyncio.wait_for(provider.complete(request), timeout=timeout)
                except TimeoutError:
                    error = ProviderError(ProviderErrorKind.TIMEOUT, f"no response within {timeout}s", provider=model.provider)
                except ProviderError as exc:
                    error = exc
                except Exception:  # unexpected adapter bug: logged with traceback, never swallowed silently
                    logger.exception("provider.unexpected_error model=%s", model.id)
                    error = ProviderError(ProviderErrorKind.UNKNOWN, "unexpected provider error", provider=model.provider)
                else:
                    error = None
                    set_attributes(span, **{"llm.outcome": "success", "llm.usage.input_tokens": response.input_tokens,
                                            "llm.usage.output_tokens": response.output_tokens})
                if error is not None:
                    # error kind only - provider messages never go into spans
                    set_attributes(span, **{"llm.outcome": "error", "llm.retryable": error.retryable})
                    mark_error(span, str(error.kind), **{"llm.error_kind": error.kind})

            if reservation is not None:
                await self._settle_budget(model, cmd, response if error is None else None, error, reservation)

            # Health updates are siblings of the attempt span (the attempt span covers only the provider call).
            if error is None:
                elapsed = time.perf_counter() - started
                self.metrics.provider_attempt(model.id, model.provider, "success", None, elapsed)
                state = await self._record_health(model.id, True)
                if post_state is not None and state is not None:
                    post_state[model.id] = state
                attempts.append(AttemptRecord(len(attempts) + 1, model.id, model.provider, "success", None, None, _ms(started)))
                log_event(
                    logger, "provider.success", model=model.id, latency_ms=_ms(started),
                    input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                )
                return response, None

            self.metrics.provider_attempt(model.id, model.provider, "error", error.kind, time.perf_counter() - started)
            attempts.append(
                AttemptRecord(len(attempts) + 1, model.id, model.provider, "error", error.kind, error.retryable, _ms(started))
            )
            if error.affects_health:
                await self._record_health(model.id, False, error.kind)
            log_event(
                logger, "provider.failure", logging.WARNING, model=model.id, error_kind=error.kind,
                retryable=error.retryable, status_code=error.status_code, latency_ms=_ms(started),
                detail=error.message,
            )
            if not error.retryable:
                break
        return None, error

    async def _settle_budget(self, model: ModelConfig, cmd: ChatCommand, response: ProviderResponse | None,
                             error: ProviderError | None, reservation: Reservation) -> None:
        assert self.budget is not None
        if response is not None:
            usage = build_usage(reported_input=response.input_tokens, reported_output=response.output_tokens,
                                prompt_text=cmd.query, completion_text=response.content,
                                estimate_missing=self.execution.estimate_missing_usage)
            cost = estimate_cost(usage, model).total_cost
            await self.budget.settle(reservation, cost_usd=cost, tokens=usage.total_tokens)
        elif error is not None and error.kind in NOT_BILLED:
            await self.budget.settle(reservation, cost_usd=0.0, tokens=0)
        # timeout / 5xx / connection / unknown: keep the full reservation (may have been billed)

    @staticmethod
    def _fallback_reason(attempts: list[AttemptRecord], original: str) -> str:
        kinds = [a.error_kind for a in attempts if a.model_id == original and a.error_kind]
        return f"Primary model '{original}' failed ({', '.join(dict.fromkeys(kinds)) or 'unavailable'})."
