"""Read-only budget status (GET /api/v1/budget/status) and process-counter readers.

Authoritative sources (nothing here is estimated or invented):
  limits / flags      Settings, via the SAME BudgetLimits the guard enforces (routing.factory.budget_limits)
  today's spend       the budget guard's own store (Redis: one MGET; memory: dict lookup). Read-only:
                      no INCRBY / PEXPIRE / EVAL, so TTLs and amounts are never touched by a status read.
  embedding / refusal Prometheus counters of THIS process (labelled as such; never durable history)

Unknown values are null with a reason; a zero is only reported when the source says zero.
"""

import logging
from datetime import UTC, date, datetime, timedelta

from app.budget.guard import MICROS_PER_USD, BudgetGuard
from app.core.config import Settings
from app.core.logging import log_event
from app.infra.redis import REDIS_ERRORS
from app.observability.metrics import Metrics
from app.routing.factory import budget_limits
from app.schemas.budget import (
    BudgetEnforcement,
    BudgetLimitsOut,
    BudgetMonthly,
    BudgetPeriod,
    BudgetStatusResponse,
    BudgetToday,
    EmbeddingBudgetOut,
    ProcessCounterGroup,
)

logger = logging.getLogger("app.budget_view")

APPLIES_TO = "real provider calls and paid embeddings only (mock calls never reserve or count spend)"


def _none_if_zero(value):
    return value if value else None


def period_for(now: datetime) -> BudgetPeriod:
    start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return BudgetPeriod(day=start.date(), starts_at=start, resets_at=start + timedelta(days=1))


def process_counter_groups(metrics: Metrics | None, counter_attr: str) -> list[ProcessCounterGroup] | None:
    """Samples of one labelled counter of this process; None when metrics are disabled."""
    if metrics is None or not metrics.enabled:
        return None
    counter = getattr(metrics, counter_attr)
    groups = []
    for family in counter.collect():
        for sample in family.samples:
            if sample.name.endswith("_total") and sample.value:
                groups.append(ProcessCounterGroup(labels=dict(sorted(sample.labels.items())),
                                                  value=round(float(sample.value), 10)))
    return sorted(groups, key=lambda g: sorted(g.labels.items()))


def process_counter_value(metrics: Metrics | None, sample_name: str) -> float | None:
    if metrics is None or not metrics.enabled:
        return None
    value = metrics.registry.get_sample_value(sample_name)
    return round(float(value), 10) if value is not None else None


async def build_budget_status(settings: Settings, guard: BudgetGuard | None, metrics: Metrics | None, *,
                              now: datetime | None = None) -> BudgetStatusResponse:
    limits = budget_limits(settings)
    paid_embeddings = settings.embedding_provider == "litellm"
    notes: list[str] = []

    if guard is not None:
        day_key, instant = guard.period()  # one clock read: period and counters always agree
    else:
        instant = now or datetime.now(UTC)
        day_key = instant.strftime("%Y%m%d")
    period = period_for(instant)

    counted: float | None = None
    remaining: float | None = None
    reason: str | None = None
    if guard is None:
        reason = ("no spend limit is configured, so spend is not tracked" if not limits.configured
                  else "the budget guard is not active in this process")
    elif not limits.tracks_daily:
        reason = "only a per-request limit is configured; daily spend is not tracked"
    else:
        try:
            totals = await guard.store.totals(day_key)
        except REDIS_ERRORS as exc:
            log_event(logger, "budget_view.store_unavailable", logging.WARNING, error=type(exc).__name__)
            reason = "the budget store (Redis) is unreachable"
        else:
            counted = round(totals["global_usd_micros"] / MICROS_PER_USD, 6)
            if limits.daily_usd:
                remaining = round(max(0.0, limits.daily_usd - counted), 6)
    if guard is not None and guard.store.name == "memory":
        notes.append("Budget counters are process-local (REDIS_ENABLED=false): other instances keep their own.")
    if limits.tracks_daily and counted is not None and not (settings.real_provider_calls_enabled or paid_embeddings):
        notes.append("Real provider calls and paid embeddings are disabled: nothing can be counted against the budget.")

    store = guard.store.name if guard is not None else None
    return BudgetStatusResponse(
        period=period,
        enforcement=BudgetEnforcement(
            configured=limits.configured, daily_tracking=limits.tracks_daily, applies_to=APPLIES_TO,
            currently_applicable=limits.configured and (settings.real_provider_calls_enabled or paid_embeddings),
            mock_mode=settings.llm_mock_mode, real_provider_calls_enabled=settings.real_provider_calls_enabled,
            paid_embeddings_enabled=paid_embeddings, fail_closed=settings.budget_fail_closed, store=store,
            store_scope=None if store is None else
            ("shared_across_instances" if store == "redis" else "this_process_only"),
        ),
        limits=BudgetLimitsOut(
            daily_usd=_none_if_zero(limits.daily_usd), daily_usd_per_client=_none_if_zero(limits.daily_usd_per_client),
            daily_tokens_per_client=_none_if_zero(limits.daily_tokens_per_client),
            max_cost_per_request_usd=_none_if_zero(limits.max_cost_per_request_usd),
        ),
        today=BudgetToday(available=counted is not None, counted_usd=counted, remaining_usd=remaining,
                          unavailable_reason=reason),
        monthly=BudgetMonthly(note="The backend enforces daily limits only; no monthly limit exists or is derived."),
        embedding=EmbeddingBudgetOut(
            provider=settings.embedding_provider, paid=paid_embeddings, budgeted=paid_embeddings,
            process_counters_available=metrics is not None and metrics.enabled,
            process_estimated_cost_usd=process_counter_value(metrics, "llm_router_embedding_estimated_cost_usd_total"),
            process_events=process_counter_groups(metrics, "embedding_budget_events"),
        ),
        notes=notes,
    )


def default_range(start: date | None, end: date | None, *, today: date, default_days: int) -> tuple[date, date]:
    """Inclusive UTC-day range. Defaults: the last `default_days` days ending today; a lone start
    gets `default_days` days (ending no later than today); a lone end gets the days before it."""
    span = timedelta(days=default_days - 1)
    if end is None:
        end = today if start is None else min(start + span, today)
    if start is None:
        start = end - span
    return start, end
