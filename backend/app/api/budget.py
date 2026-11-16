"""Read-only budget endpoints (observability token, like /analytics and /settings):

  GET /api/v1/budget/status    limits, today's counted spend (budget guard counters), period, embeddings
  GET /api/v1/budget/spend     persisted per-request cost estimates per UTC day / provider / model
  GET /api/v1/budget/refusals  persisted budget refusals of chat attempts (paginated)

Nothing here reserves, settles or otherwise writes budget state: enforcement stays in app/budget/guard.py.
Costs are estimates (mock rows: placeholder prices, simulated) - never provider invoices.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.analytics import DatabaseUnavailableAPIError, InvalidTimeRangeError
from app.api.deps import get_container, require_observability_token
from app.budget.guard import REASONS
from app.core.budget_view import build_budget_status, default_range, process_counter_groups
from app.core.logging import log_event
from app.db.budget_queries import BUDGET_ERROR_CODES, refusal_summary, spend_summary
from app.infra.container import Container
from app.infra.database import DB_ERRORS, DatabaseUnavailableError
from app.schemas.budget import (
    BudgetRefusalsResponse,
    BudgetSpendResponse,
    BudgetStatusResponse,
    Pagination,
    RefusalCount,
    RefusalEvent,
    SpendDay,
    SpendFigures,
    SpendGroup,
)

router = APIRouter(prefix="/budget", tags=["budget"], dependencies=[Depends(require_observability_token)])
logger = logging.getLogger("app.budget_api")

MAX_RANGE_DAYS = 92
DEFAULT_SPEND_DAYS = 30
DEFAULT_REFUSAL_DAYS = 7
MAX_PAGE_SIZE = 100

CATEGORY = {
    "per_request_limit": "per_request",
    "global_daily_budget": "global_daily",
    "client_daily_budget": "client_daily",
    "client_daily_tokens": "client_daily_tokens",
    "budget_unavailable": "store_unavailable",
    "other": "other",
}
DESCRIPTION = {
    "per_request_limit": "Worst-case cost of the request exceeded BUDGET_MAX_COST_PER_REQUEST_USD.",
    "global_daily_budget": "The global daily USD limit would have been exceeded.",
    "client_daily_budget": "A client's daily USD limit would have been exceeded.",
    "client_daily_tokens": "A client's daily token limit would have been exceeded.",
    "budget_unavailable": "The budget store was unreachable and BUDGET_FAIL_CLOSED=true.",
    "other": "Refused by the budget guard (unrecognised reason).",
}
DateParam = Annotated[date | None, Query(description="UTC day, YYYY-MM-DD (inclusive).")]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"  # explicit, independent of SECURITY_HEADERS_ENABLED


def _reason(value: str | None) -> str:
    return value if value in REASONS else "other"


def _bounds(start: date | None, end: date | None, default_days: int) -> tuple[date, date, datetime, datetime]:
    today = datetime.now(UTC).date()
    start, end = default_range(start, end, today=today, default_days=default_days)
    if start > end:
        raise InvalidTimeRangeError("start_date must not be after end_date.")
    if end > today:
        raise InvalidTimeRangeError("end_date must not be in the future (UTC).")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise InvalidTimeRangeError(f"The range must not exceed {MAX_RANGE_DAYS} days.")
    lo = datetime(start.year, start.month, start.day, tzinfo=UTC)
    hi = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    return start, end, lo, hi


async def _query(container: Container, fn, event: str):
    db = container.database
    if not db.available:
        raise DatabaseUnavailableAPIError("Database is disabled or unreachable.")
    try:
        return await db.run(fn)
    except (*DB_ERRORS, DatabaseUnavailableError) as exc:
        log_event(logger, event, logging.ERROR, error=type(exc).__name__)
        raise DatabaseUnavailableAPIError("Database query failed.") from exc


# --------------------------------------------------------------------------- status
@router.get("/status", response_model=BudgetStatusResponse,
            summary="Configured daily limits, today's counted spend and the current budget period")
async def budget_status(container: Annotated[Container, Depends(get_container)],
                        response: Response) -> BudgetStatusResponse:
    _no_store(response)
    return await build_budget_status(container.settings, container.chat_service.budget, container.metrics)


# --------------------------------------------------------------------------- spend
def _cost_basis(t: dict) -> str:
    if not t["requests"]:
        return "no persisted requests in this range"
    if t["real_requests"] and t["mock_requests"]:
        return "mixed: mock rows are simulated at placeholder prices; real rows are estimates, not invoices"
    if t["real_requests"]:
        return "real-call estimates (tokens x configured prices); not provider invoices"
    return "mock mode only: simulated at placeholder prices, no money was spent"


@router.get("/spend", response_model=BudgetSpendResponse,
            summary="Estimated cost per UTC day, provider and model from persisted request history")
async def budget_spend(container: Annotated[Container, Depends(get_container)], response: Response,
                       start_date: DateParam = None, end_date: DateParam = None) -> BudgetSpendResponse:
    _no_store(response)
    start, end, lo, hi = _bounds(start_date, end_date, DEFAULT_SPEND_DAYS)
    result = await _query(container, lambda s: spend_summary(s, lo, hi), "budget.spend_query_failed")
    zero = {k: 0 for k in SpendFigures.model_fields}
    days = [SpendDay(date=start + timedelta(days=i), **result.days.get(start + timedelta(days=i), zero))
            for i in range((end - start).days + 1)]
    return BudgetSpendResponse(
        start_date=start, end_date=end, cost_basis=_cost_basis(result.totals), totals=SpendFigures(**result.totals),
        days=days,
        by_provider=[SpendGroup(**g) for g in result.by_provider], by_provider_truncated=result.by_provider_truncated,
        by_model=[SpendGroup(**g) for g in result.by_model], by_model_truncated=result.by_model_truncated,
        notes=[
            "Costs are per-request estimates from persisted history, never provider invoices.",
            "Only the attempt that produced the answer is costed. Failed attempts that a provider may still have "
            "billed (timeouts, 5xx) are counted by the budget guard (GET /budget/status), not here.",
            "Paid embedding spend is not persisted and is not included.",
            "Days without persisted requests show 0. Requests are missing for periods when persistence was "
            "disabled or the database was unreachable.",
        ],
    )


# --------------------------------------------------------------------------- refusals
def _outcome(status: str | None, code: str | None) -> str:
    if status == "success":
        return "served_by_another_model"
    return "refused" if code in BUDGET_ERROR_CODES else "failed"


@router.get("/refusals", response_model=BudgetRefusalsResponse,
            summary="Persisted budget refusals of chat attempts (paginated, bounded date range)")
async def budget_refusals(
    container: Annotated[Container, Depends(get_container)], response: Response,
    start_date: DateParam = None, end_date: DateParam = None,
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
) -> BudgetRefusalsResponse:
    _no_store(response)
    start, end, lo, hi = _bounds(start_date, end_date, DEFAULT_REFUSAL_DAYS)
    offset = (page - 1) * page_size
    result = await _query(container, lambda s: refusal_summary(s, lo, hi, offset=offset, limit=page_size),
                          "budget.refusals_query_failed")

    by_reason: dict[str, int] = {}
    for kind, count in result.by_reason:
        by_reason[_reason(kind)] = by_reason.get(_reason(kind), 0) + int(count)
    embedding = process_counter_groups(container.metrics, "embedding_budget_events")
    return BudgetRefusalsResponse(
        start_date=start, end_date=end,
        persisted_scope="chat provider attempts refused by the budget guard (provider_attempt outcome skipped_budget)",
        total_refused_attempts=result.total, refused_requests=result.refused_requests,
        by_reason=[RefusalCount(key=k, count=v) for k, v in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))],
        by_provider=[RefusalCount(key=p or "unknown", count=int(n)) for p, n in result.by_provider],
        events=[
            RefusalEvent(time=e["time"], reason=_reason(e["reason"]), category=CATEGORY[_reason(e["reason"])],
                         description=DESCRIPTION[_reason(e["reason"])], model=e["model"], provider=e["provider"],
                         attempt_number=e["attempt_number"], request_outcome=_outcome(e["status"], e["error_code"]),
                         mock_mode=e["mock_mode"])
            for e in result.events
        ],
        pagination=Pagination(page=page, page_size=page_size, total=result.total,
                              has_more=offset + len(result.events) < result.total),
        process_counters_available=container.metrics is not None and container.metrics.enabled,
        process_chat_refusals=process_counter_groups(container.metrics, "budget_rejected_total"),
        process_embedding_refusals=None if embedding is None else [
            g for g in embedding if g.labels.get("event") == "refused"
        ],
        notes=[
            "Refused paid embedding calls are not persisted: only this process's counters are shown, which are "
            "not shared between instances and reset on restart.",
            "No client identifiers, request ids, prompts or responses are returned.",
            "Events exist only for periods when persistence was enabled and the database reachable.",
        ],
    )
