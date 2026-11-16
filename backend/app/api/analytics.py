"""GET /analytics/summary - aggregates over persisted request history."""

import logging
import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import get_container, require_observability_token
from app.core.errors import AppError
from app.core.logging import log_event
from app.db.analytics import summarize
from app.infra.container import Container
from app.infra.database import DB_ERRORS, DatabaseUnavailableError
from app.observability.tracing import set_attributes, start_span
from app.schemas.infra import AnalyticsSummaryResponse

router = APIRouter(prefix="/analytics", tags=["analytics"])
logger = logging.getLogger("app.analytics")


class DatabaseUnavailableAPIError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "database_unavailable"


class InvalidTimeRangeError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_time_range"


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@router.get("/summary", response_model=AnalyticsSummaryResponse, summary="Usage, cost, cache and fallback summary",
            dependencies=[Depends(require_observability_token)])
async def analytics_summary(
    container: Annotated[Container, Depends(get_container)],
    start: Annotated[datetime | None, Query(description="Inclusive start (ISO 8601; naive = UTC).")] = None,
    end: Annotated[datetime | None, Query(description="Exclusive end (ISO 8601; naive = UTC).")] = None,
) -> AnalyticsSummaryResponse:
    start, end = _utc(start), _utc(end)
    if start and end and start >= end:
        raise InvalidTimeRangeError("start must be earlier than end.")
    db = container.database
    if not db.available:
        raise DatabaseUnavailableAPIError("Database is disabled or unreachable.")

    t0 = time.perf_counter()
    try:
        with start_span(container.tracing.tracer, "db.analytics_summary", **{
            "db.system": "postgresql", "db.operation": "SELECT", "analytics.filtered": bool(start or end),
        }) as span:
            summary = await db.run(lambda session: summarize(session, start, end))
            set_attributes(span, **{"analytics.total_requests": summary.total_requests})
    except (*DB_ERRORS, DatabaseUnavailableError) as exc:
        log_event(logger, "analytics.query_failed", logging.ERROR, error=type(exc).__name__)
        raise DatabaseUnavailableAPIError("Database query failed.") from exc
    log_event(logger, "analytics.query", total_requests=summary.total_requests,
              filtered=bool(start or end), latency_ms=round((time.perf_counter() - t0) * 1000, 2))
    return AnalyticsSummaryResponse.model_validate(summary)
