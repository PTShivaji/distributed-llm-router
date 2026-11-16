"""Prometheus metrics (prometheus_client), one isolated registry per app instance.

Label policy - every label has a small, code- or config-defined value set:
  method        standard HTTP methods, else "OTHER"
  route         route template from code (e.g. /api/v1/chat) or "unmatched"
  status        HTTP status code (bounded 100-599)
  model         model ids from the registry (unknown overrides are rejected
                before any metric is recorded); "none" when no model was used
  provider      provider names from the registry
  outcome       fixed enums;  error_kind  ProviderErrorKind values or "none"
  complexity    SIMPLE | MEDIUM | COMPLEX
  cache_status  fixed CacheStatus enum
  category/code error categories / known error codes, else "other"
NEVER used as labels: prompts, raw paths/URLs, query strings, request ids,
client ids, IP addresses or any other user-controlled text.

Each process keeps its own counters; Prometheus scrapes each instance and
aggregates (sum by ...) across instances.
"""

import math
from collections.abc import Iterable

import prometheus_client
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from app.core.errors import CATEGORY_VALUES, ERROR_CATEGORIES

# Drop the optional "<name>_created" timestamp series: they double scrape size
# and add nothing PromQL rate()/increase() needs.
prometheus_client.disable_created_metrics()

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
CACHE_STATUSES = frozenset({"disabled", "bypass", "miss", "hit_exact", "hit_semantic", "error"})
CACHE_OPERATIONS = frozenset({"lookup", "store"})
DB_RECONNECT_OUTCOMES = frozenset({"success", "failure"})
DB_PERSIST_OUTCOMES = frozenset({"written", "deduplicated", "failed", "dropped_queue_full", "dropped_shutdown",
                                 "dropped_not_started"})
CACHE_DEGRADED_REASONS = frozenset({"redis_unavailable", "pool_exhausted", "oom", "global_cap", "entry_too_large",
                                    "embedding_failed"})
ATTEMPT_OUTCOMES = frozenset({"success", "error", "skipped_unhealthy", "skipped_rate_limited", "skipped_budget"})
EMBEDDING_BUDGET_EVENTS = frozenset({"reserved", "refused", "settled", "released", "kept_uncertain",
                                     "kept_no_usage"})
BUDGET_REASONS = frozenset({"per_request_limit", "global_daily_budget", "client_daily_budget",
                            "client_daily_tokens", "budget_unavailable"})
COMPLEXITIES = frozenset({"SIMPLE", "MEDIUM", "COMPLEX"})
USAGE_SOURCES = frozenset({"provider_reported", "estimated", "partially_estimated"})
ERROR_CODES = frozenset(ERROR_CATEGORIES) | {"http_error"}

# Latency buckets (seconds): sub-ms in-process work up to slow LLM calls.
HTTP_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
PROVIDER_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)


def _bounded(value: object, allowed: Iterable[str], default: str = "other") -> str:
    text = str(value) if value is not None else ""
    return text if text in allowed else default


class Metrics:
    """Thin facade so call sites stay one-liners and label rules live in one place."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        registry: CollectorRegistry | None = None,
        model_ids: Iterable[str] = (),
        providers: Iterable[str] = (),
        app_version: str = "unknown",
        mock_mode: bool = True,
    ) -> None:
        self.enabled = enabled
        self.registry = registry or CollectorRegistry()
        self.model_ids = frozenset(model_ids)
        self.providers = frozenset(providers)
        r = self.registry

        self.build_info = Gauge("llm_router_build_info", "Build information (value is always 1).",
                                ["version", "mock_mode"], registry=r)
        self.build_info.labels(version=app_version, mock_mode=str(mock_mode).lower()).set(1)

        # --- HTTP ---
        self.http_requests = Counter("llm_router_http_requests_total", "HTTP requests handled.",
                                     ["method", "route", "status"], registry=r)
        self.http_duration = Histogram("llm_router_http_request_duration_seconds", "HTTP request latency.",
                                       ["method", "route"], buckets=HTTP_BUCKETS, registry=r)
        self.http_in_progress = Gauge("llm_router_http_requests_in_progress", "HTTP requests in flight.",
                                      registry=r)
        self.errors = Counter("llm_router_errors_total", "Failed HTTP requests by error category and code.",
                              ["category", "code"], registry=r)

        # --- Routing / providers ---
        self.chat_requests = Counter(
            "llm_router_chat_requests_total", "Chat requests by classification, routing and cache outcome.",
            ["complexity", "selected_model", "model_used", "cache_status", "outcome"], registry=r)
        self.provider_attempts = Counter(
            "llm_router_provider_attempts_total", "Provider attempts (incl. skipped candidates).",
            ["model", "provider", "outcome", "error_kind"], registry=r)
        self.provider_latency = Histogram(
            "llm_router_provider_latency_seconds", "Latency of provider invocations (success or failure).",
            ["model", "provider", "outcome"], buckets=PROVIDER_BUCKETS, registry=r)
        self.retries = Counter("llm_router_retries_total", "Retries of the same model after a transient error.",
                               ["model"], registry=r)
        self.fallbacks = Counter("llm_router_fallbacks_total", "Fallback attempts from the primary to another model.",
                                 ["from_model", "to_model"], registry=r)

        # --- Cache ---
        self.cache_lookups = Counter("llm_router_cache_lookups_total", "Response-cache lookups by result.",
                                     ["status"], registry=r)
        self.cache_hit_ratio = Gauge(
            "llm_router_cache_hit_ratio",
            "hits / (hits + misses) for this process since start (prefer rate() on cache_lookups_total).",
            registry=r)
        self.cache_hit_ratio.set(math.nan)  # undefined until the first hit/miss
        # Phase 11.1: cache work that did not happen, and why (Redis failure classes, global cap, size limit).
        self.cache_degraded_total = Counter(
            "llm_router_cache_degraded_total",
            "Cache lookups/stores that degraded or were refused, by operation and reason.",
            ["operation", "reason"], registry=r)

        # --- PostgreSQL availability / persistence (Phase 11.2, H2) ---
        self.db_available = Gauge("llm_router_db_available",
                                  "1 when PostgreSQL is connected and migrated, 0 while unavailable/reconnecting.",
                                  registry=r)
        self.db_reconnect_attempts = Counter("llm_router_db_reconnect_attempts_total",
                                             "Background PostgreSQL reconnect attempts by outcome.",
                                             ["outcome"], registry=r)
        self.db_persist_records_total = Counter(
            "llm_router_db_persist_records_total",
            "Request-history records by outcome (written, deduplicated on retry, failed, dropped_*).",
            ["outcome"], registry=r)
        self.db_write_retries = Counter("llm_router_db_write_retries_total",
                                        "Persistence batch writes retried after a connection-level error.",
                                        registry=r)
        self._cache_hits = 0
        self._cache_misses = 0

        # --- Usage / cost ---
        self.tokens = Counter("llm_router_tokens_total", "Tokens consumed by provider calls (cache hits excluded).",
                              ["model", "direction", "source"], registry=r)
        self.cost = Counter("llm_router_estimated_cost_usd_total",
                            "Estimated cost in USD from registry pricing (pricing=placeholder in mock mode).",
                            ["model", "pricing"], registry=r)

        # --- Rate limiting ---
        self.rate_limited = Counter("llm_router_rate_limit_rejections_total", "Requests or model calls rejected by rate limits.",
                                    ["scope", "window"], registry=r)

        # --- Spend safeguards (step 8) ---
        self.budget_rejected_total = Counter(
            "llm_router_budget_rejections_total", "Real provider attempts refused by spend safeguards.",
            ["reason"], registry=r)
        # Step 8A: paid embedding calls (reason="none" except for refusals). Both labels bounded.
        self.embedding_budget_events = Counter(
            "llm_router_embedding_budget_events_total",
            "Budget outcomes of paid embedding calls (reserved/refused/settled/released/kept_*).",
            ["event", "reason"], registry=r)
        self.embedding_cost_usd = Counter(
            "llm_router_embedding_estimated_cost_usd_total",
            "Estimated USD of paid embedding calls with provider-reported usage.", registry=r)

    # ------------------------------------------------------------------ label helpers
    def _model(self, model_id: str | None) -> str:
        if not model_id:
            return "none"
        return model_id if (not self.model_ids or model_id in self.model_ids) else "other"

    def _provider(self, provider: str | None) -> str:
        if not provider:
            return "none"
        return provider if (not self.providers or provider in self.providers) else "other"

    # ------------------------------------------------------------------ recorders
    def observe_http(self, method: str, route: str, status: int, seconds: float,
                     error_category: str | None, error_code: str | None) -> None:
        if not self.enabled:
            return
        m = _bounded(method, HTTP_METHODS, "OTHER")
        self.http_requests.labels(method=m, route=route, status=str(int(status))).inc()
        self.http_duration.labels(method=m, route=route).observe(seconds)
        if status >= 400 or error_category:
            self.errors.labels(
                category=_bounded(error_category or ("internal" if status >= 500 else "client"), CATEGORY_VALUES),
                code=_bounded(error_code or ("internal_error" if status >= 500 else "http_error"), ERROR_CODES),
            ).inc()

    def provider_attempt(self, model: str, provider: str, outcome: str, error_kind: str | None,
                         seconds: float | None) -> None:
        if not self.enabled:
            return
        labels = dict(model=self._model(model), provider=self._provider(provider))
        self.provider_attempts.labels(**labels, outcome=_bounded(outcome, ATTEMPT_OUTCOMES),
                                      error_kind=str(error_kind) if error_kind else "none").inc()
        if seconds is not None:
            self.provider_latency.labels(**labels, outcome="success" if outcome == "success" else "error").observe(seconds)

    def retry(self, model: str) -> None:
        if self.enabled:
            self.retries.labels(model=self._model(model)).inc()

    def fallback(self, from_model: str, to_model: str) -> None:
        if self.enabled:
            self.fallbacks.labels(from_model=self._model(from_model), to_model=self._model(to_model)).inc()

    def db_set_available(self, available: bool) -> None:
        if self.enabled:
            self.db_available.set(1 if available else 0)

    def db_reconnect_attempt(self, outcome: str) -> None:
        if self.enabled:
            self.db_reconnect_attempts.labels(outcome=_bounded(outcome, DB_RECONNECT_OUTCOMES)).inc()

    def db_persist_records(self, outcome: str, n: int = 1) -> None:
        if self.enabled:
            self.db_persist_records_total.labels(outcome=_bounded(outcome, DB_PERSIST_OUTCOMES)).inc(n)

    def db_write_retry(self) -> None:
        if self.enabled:
            self.db_write_retries.inc()

    def cache_degraded(self, operation: str, reason: str) -> None:
        if self.enabled:
            self.cache_degraded_total.labels(operation=_bounded(operation, CACHE_OPERATIONS),
                                             reason=_bounded(reason, CACHE_DEGRADED_REASONS)).inc()

    def cache_lookup(self, status: str) -> None:
        if not self.enabled:
            return
        status = _bounded(status, CACHE_STATUSES)
        self.cache_lookups.labels(status=status).inc()
        if status in ("hit_exact", "hit_semantic"):
            self._cache_hits += 1
        elif status == "miss":
            self._cache_misses += 1
        else:
            return
        self.cache_hit_ratio.set(self._cache_hits / (self._cache_hits + self._cache_misses))

    def chat_completed(self, *, complexity: str | None, selected_model: str | None, model_used: str | None,
                       cache_status: str, outcome: str) -> None:
        if self.enabled:
            self.chat_requests.labels(
                complexity=_bounded(complexity, COMPLEXITIES, "unknown"),
                selected_model=self._model(selected_model), model_used=self._model(model_used),
                cache_status=_bounded(cache_status, CACHE_STATUSES), outcome="success" if outcome == "success" else "error",
            ).inc()

    def usage(self, model: str, input_tokens: int | None, output_tokens: int | None, source: str,
              cost_usd: float | None, pricing_is_placeholder: bool) -> None:
        if not self.enabled or source not in USAGE_SOURCES:  # cache hits / unavailable: nothing consumed/known
            return
        model_label = self._model(model)
        if input_tokens:
            self.tokens.labels(model=model_label, direction="input", source=source).inc(input_tokens)
        if output_tokens:
            self.tokens.labels(model=model_label, direction="output", source=source).inc(output_tokens)
        if cost_usd:
            self.cost.labels(model=model_label, pricing="placeholder" if pricing_is_placeholder else "configured").inc(cost_usd)

    def embedding_budget(self, event: str, reason: str | None = None) -> None:
        if self.enabled:
            self.embedding_budget_events.labels(event=_bounded(event, EMBEDDING_BUDGET_EVENTS),
                                                reason="none" if reason is None
                                                else _bounded(reason, BUDGET_REASONS)).inc()

    def embedding_cost(self, usd: float) -> None:
        if self.enabled and usd > 0:
            self.embedding_cost_usd.inc(usd)

    def budget_rejected(self, reason: str | None) -> None:
        if self.enabled:
            self.budget_rejected_total.labels(reason=_bounded(reason, BUDGET_REASONS, "other")).inc()

    def rate_limit_rejected(self, scope: str, window: str | None) -> None:
        if self.enabled:
            self.rate_limited.labels(scope=_bounded(scope, {"client", "model"}),
                                     window=_bounded(window, {"minute", "hour"}, "unknown")).inc()
