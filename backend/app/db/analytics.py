"""Aggregations for GET /api/v1/analytics/summary (all computed in SQL)."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.models import ProviderAttempt, RequestLog

CACHE_HITS = ("hit_exact", "hit_semantic")


@dataclass
class AnalyticsSummary:
    start: datetime | None
    end: datetime | None
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    estimated_total_cost: float = 0.0
    average_latency_ms: float | None = None
    cache_hits: int = 0
    cache_misses: int = 0
    cache_hit_rate: float | None = None
    fallback_requests: int = 0
    fallback_rate: float | None = None
    requests_by_complexity: dict[str, int] = field(default_factory=dict)
    requests_by_model: list[dict] = field(default_factory=list)
    provider_attempts: list[dict] = field(default_factory=list)


def _window(stmt: Select, column, start: datetime | None, end: datetime | None) -> Select:
    if start is not None:
        stmt = stmt.where(column >= start)
    if end is not None:
        stmt = stmt.where(column < end)
    return stmt


def summarize(session: Session, start: datetime | None, end: datetime | None) -> AnalyticsSummary:
    s = AnalyticsSummary(start=start, end=end)
    r = RequestLog

    totals = session.execute(_window(select(
        func.count(),
        func.count().filter(r.status == "success"),
        func.coalesce(func.sum(r.input_tokens), 0),
        func.coalesce(func.sum(r.output_tokens), 0),
        func.coalesce(func.sum(r.total_tokens), 0),
        func.coalesce(func.sum(r.total_cost), 0),
        func.avg(r.latency_ms),
        func.count().filter(r.cache_status.in_(CACHE_HITS)),
        func.count().filter(r.cache_status == "miss"),
        func.count().filter(r.fallback_used.is_(True)),
    ), r.created_at, start, end)).one()

    (s.total_requests, s.successful_requests, s.total_input_tokens, s.total_output_tokens, s.total_tokens,
     cost, avg_latency, s.cache_hits, s.cache_misses, s.fallback_requests) = totals
    s.failed_requests = s.total_requests - s.successful_requests
    s.estimated_total_cost = round(float(cost), 10)
    s.average_latency_ms = round(float(avg_latency), 3) if avg_latency is not None else None
    lookups = s.cache_hits + s.cache_misses
    s.cache_hit_rate = round(s.cache_hits / lookups, 4) if lookups else None
    s.fallback_rate = round(s.fallback_requests / s.total_requests, 4) if s.total_requests else None

    by_complexity = session.execute(
        _window(select(r.complexity, func.count()).group_by(r.complexity), r.created_at, start, end)
    ).all()
    s.requests_by_complexity = {c or "unknown": n for c, n in by_complexity}

    by_model = session.execute(_window(select(
        r.model_used, func.count(), func.coalesce(func.sum(r.total_tokens), 0),
        func.coalesce(func.sum(r.total_cost), 0), func.avg(r.latency_ms),
    ).where(r.model_used.is_not(None)).group_by(r.model_used).order_by(func.count().desc()),
        r.created_at, start, end)).all()
    s.requests_by_model = [
        {"model": m, "requests": n, "total_tokens": int(t), "estimated_cost": round(float(c), 10),
         "average_latency_ms": round(float(lat), 3) if lat is not None else None}
        for m, n, t, c, lat in by_model
    ]

    a = ProviderAttempt
    attempts = session.execute(_window(select(
        a.provider,
        func.count().filter(a.outcome == "success"),
        func.count().filter(a.outcome == "error"),
        func.count().filter(a.outcome.like("skipped%")),
    ).group_by(a.provider).order_by(a.provider), a.created_at, start, end)).all()
    s.provider_attempts = [
        {"provider": p, "successes": ok, "failures": err, "skipped": skip} for p, ok, err, skip in attempts
    ]
    return s
