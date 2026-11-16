"""Response schemas for the read-only budget endpoints (GET /api/v1/budget/*).

Every model forbids extra fields (explicit allow-list). None of these schemas has a field for
prompts, responses, client identifiers (raw or hashed), request ids, keys, tokens, passwords,
URLs or hosts, so none of that can be returned.

Terminology used throughout:
  counted_usd          amount counted against the daily limit by the budget guard (reservations of
                       in-flight calls + settled amounts + reservations KEPT because a failed call may
                       have been billed). It is the enforcement figure, not a provider invoice.
  *_estimated_cost_usd persisted per-request estimates (reported/estimated tokens x configured price).
                       mock rows use placeholder prices: simulated, no money is involved.
  process counters     Prometheus counters of THIS API process since it started: not shared between
                       instances, reset on restart, never durable history.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BudgetReason = Literal["per_request_limit", "global_daily_budget", "client_daily_budget", "client_daily_tokens",
                       "budget_unavailable", "other"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- status
class BudgetPeriod(_Strict):
    timezone: Literal["UTC"] = "UTC"
    granularity: Literal["day"] = "day"
    day: date = Field(description="Current budget day (UTC).")
    starts_at: datetime
    resets_at: datetime = Field(description="Next UTC midnight: daily counters start from zero for the new day.")


class BudgetEnforcement(_Strict):
    configured: bool = Field(description="At least one spend limit is set.")
    daily_tracking: bool = Field(description="Daily counters are maintained (a daily USD or token limit is set).")
    applies_to: str
    currently_applicable: bool = Field(
        description="Limits can only be reached when real provider calls or paid embeddings are enabled.")
    mock_mode: bool
    real_provider_calls_enabled: bool
    paid_embeddings_enabled: bool
    fail_closed: bool
    store: Literal["redis", "memory"] | None = Field(description="null when no budget guard is active.")
    store_scope: Literal["shared_across_instances", "this_process_only"] | None


class BudgetLimitsOut(_Strict):
    daily_usd: float | None = Field(description="Global daily limit; null = not configured.")
    daily_usd_per_client: float | None = Field(description="null = not configured.")
    daily_tokens_per_client: int | None = Field(description="null = not configured.")
    max_cost_per_request_usd: float | None = Field(description="Worst-case cap per request; null = not configured.")


class BudgetToday(_Strict):
    available: bool
    source: Literal["budget_counters"] = "budget_counters"
    counted_usd: float | None = Field(description="Global amount counted against today's limits; null = unknown.")
    remaining_usd: float | None = Field(
        description="daily_usd - counted_usd (never below 0); null when no global daily limit or unknown.")
    unavailable_reason: str | None
    per_client_counters_exposed: Literal[False] = Field(
        default=False, description="Per-client counters exist but are never exposed (client identities).")


class BudgetMonthly(_Strict):
    enforcement_supported: Literal[False] = False
    limit_usd: None = None
    note: str


class ProcessCounterGroup(_Strict):
    labels: dict[str, str]
    value: float


class EmbeddingBudgetOut(_Strict):
    provider: str
    paid: bool = Field(description="True for EMBEDDING_PROVIDER=litellm; the local embedder is free.")
    budgeted: bool = Field(description="Paid embeddings reserve against the same daily counters as chat.")
    persisted_history: Literal[False] = Field(
        default=False, description="Embedding spend is never written to the request history.")
    process_counters_available: bool = Field(description="False when METRICS_ENABLED=false.")
    process_estimated_cost_usd: float | None = Field(
        description="This process since start; null when metrics are disabled.")
    process_events: list[ProcessCounterGroup] | None


class BudgetStatusResponse(_Strict):
    period: BudgetPeriod
    enforcement: BudgetEnforcement
    limits: BudgetLimitsOut
    today: BudgetToday
    monthly: BudgetMonthly
    embedding: EmbeddingBudgetOut
    notes: list[str]


# --------------------------------------------------------------------------- spend
class SpendFigures(_Strict):
    requests: int = Field(description="Persisted chat requests (all outcomes).")
    successful_requests: int
    total_tokens: int
    mock_requests: int
    real_requests: int
    mock_estimated_cost_usd: float = Field(description="Simulated: mock responses at placeholder prices (no money).")
    real_estimated_cost_usd: float = Field(description="Estimate for real calls; NOT a provider invoice.")
    placeholder_priced_requests: int
    cost_unknown_requests: int = Field(description="Successful requests without a recorded cost.")


class SpendDay(SpendFigures):
    date: date


class SpendGroup(_Strict):
    name: str
    requests: int = Field(description="Successful requests served by this provider/model.")
    total_tokens: int
    mock_estimated_cost_usd: float
    real_estimated_cost_usd: float
    placeholder_priced_requests: int


class BudgetSpendResponse(_Strict):
    start_date: date
    end_date: date = Field(description="Inclusive (UTC days).")
    timezone: Literal["UTC"] = "UTC"
    source: Literal["request_history"] = "request_history"
    currency: Literal["USD"] = "USD"
    cost_basis: str
    totals: SpendFigures
    days: list[SpendDay]
    by_provider: list[SpendGroup]
    by_provider_truncated: bool
    by_model: list[SpendGroup]
    by_model_truncated: bool
    embedding_spend_included: Literal[False] = False
    notes: list[str]


# --------------------------------------------------------------------------- refusals
class RefusalEvent(_Strict):
    time: datetime
    reason: BudgetReason
    category: str
    description: str
    model: str | None
    provider: str | None
    attempt_number: int
    request_outcome: Literal["refused", "served_by_another_model", "failed"]
    mock_mode: bool


class RefusalCount(_Strict):
    key: str
    count: int


class Pagination(_Strict):
    page: int
    page_size: int
    total: int
    has_more: bool


class BudgetRefusalsResponse(_Strict):
    start_date: date
    end_date: date
    timezone: Literal["UTC"] = "UTC"
    source: Literal["request_history"] = "request_history"
    persisted_scope: str
    total_refused_attempts: int
    refused_requests: int = Field(description="Requests that failed with budget_exceeded or budget_unavailable.")
    by_reason: list[RefusalCount]
    by_provider: list[RefusalCount]
    events: list[RefusalEvent]
    pagination: Pagination
    embedding_refusals_persisted: Literal[False] = False
    process_counters_available: bool
    process_chat_refusals: list[ProcessCounterGroup] | None = Field(
        description="This process since start (not durable); null when metrics are disabled.")
    process_embedding_refusals: list[ProcessCounterGroup] | None
    notes: list[str]
