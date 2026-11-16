"""SQL aggregations for GET /api/v1/budget/spend and /budget/refusals (read-only SELECTs).

Every query is bounded: a [start, end) window of at most MAX_RANGE_DAYS UTC days (enforced by the
API), grouped results capped (MAX_GROUPS), refusal events paginated (page_size <= 100).
Never selected: prompt_text, response_text, client_hash, request_id.
Days are UTC calendar days (timezone('UTC', created_at)), the same boundary as the budget counters.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.orm import Session

from app.db.models import ProviderAttempt, RequestLog

MAX_GROUPS = 50
BUDGET_ERROR_CODES = ("budget_exceeded", "budget_unavailable")

r = RequestLog
a = ProviderAttempt


def _sum(expr):
    return func.coalesce(func.sum(expr), 0)


def _figures():
    """Columns shared by totals / days: counts and cost split mock (placeholder) vs real (estimate)."""
    success = r.status == "success"
    return (
        func.count(),
        func.count().filter(success),
        _sum(r.total_tokens),
        func.count().filter(r.mock_mode.is_(True)),
        func.count().filter(r.mock_mode.is_(False)),
        _sum(case((r.mock_mode.is_(True), r.total_cost), else_=0)),
        _sum(case((r.mock_mode.is_(False), r.total_cost), else_=0)),
        func.count().filter(and_(success, r.pricing_is_placeholder.is_(True))),
        func.count().filter(and_(success, r.total_cost.is_(None))),
    )


FIGURE_KEYS = ("requests", "successful_requests", "total_tokens", "mock_requests", "real_requests",
               "mock_estimated_cost_usd", "real_estimated_cost_usd", "placeholder_priced_requests",
               "cost_unknown_requests")


def _row_to_figures(row) -> dict:
    out = dict(zip(FIGURE_KEYS, row, strict=True))
    for k in ("mock_estimated_cost_usd", "real_estimated_cost_usd"):
        out[k] = round(float(out[k]), 10)
    for k in FIGURE_KEYS:
        if not k.endswith("_usd"):
            out[k] = int(out[k])
    return out


def _window(stmt: Select, column, start: datetime, end: datetime) -> Select:
    return stmt.where(column >= start, column < end)


@dataclass
class SpendResult:
    totals: dict
    days: dict[date, dict]
    by_provider: list[dict]
    by_provider_truncated: bool
    by_model: list[dict]
    by_model_truncated: bool


def _groups(session: Session, column, start: datetime, end: datetime) -> tuple[list[dict], bool]:
    rows = session.execute(_window(select(
        column,
        func.count(),
        _sum(r.total_tokens),
        _sum(case((r.mock_mode.is_(True), r.total_cost), else_=0)),
        _sum(case((r.mock_mode.is_(False), r.total_cost), else_=0)),
        func.count().filter(r.pricing_is_placeholder.is_(True)),
    ).where(r.status == "success", column.is_not(None)).group_by(column)
        .order_by(func.count().desc(), column).limit(MAX_GROUPS + 1), r.created_at, start, end)).all()
    groups = [
        {"name": name, "requests": int(n), "total_tokens": int(t), "mock_estimated_cost_usd": round(float(cm), 10),
         "real_estimated_cost_usd": round(float(cr), 10), "placeholder_priced_requests": int(p)}
        for name, n, t, cm, cr, p in rows[:MAX_GROUPS]
    ]
    return groups, len(rows) > MAX_GROUPS


def spend_summary(session: Session, start: datetime, end: datetime) -> SpendResult:
    day = func.date(func.timezone("UTC", r.created_at))
    totals = _row_to_figures(session.execute(_window(select(*_figures()), r.created_at, start, end)).one())
    day_rows = session.execute(_window(select(day, *_figures()).group_by(day).order_by(day),
                                       r.created_at, start, end)).all()
    days = {row[0]: _row_to_figures(row[1:]) for row in day_rows}
    by_provider, p_trunc = _groups(session, r.provider, start, end)
    by_model, m_trunc = _groups(session, r.model_used, start, end)
    return SpendResult(totals, days, by_provider, p_trunc, by_model, m_trunc)


@dataclass
class RefusalResult:
    total: int
    refused_requests: int
    by_reason: list[tuple[str | None, int]]
    by_provider: list[tuple[str | None, int]]
    events: list[dict] = field(default_factory=list)


def refusal_summary(session: Session, start: datetime, end: datetime, *, offset: int, limit: int) -> RefusalResult:
    refused = a.outcome == "skipped_budget"
    base = _window(select(func.count()).select_from(a).where(refused), a.created_at, start, end)
    total = int(session.execute(base).scalar_one())
    refused_requests = int(session.execute(_window(
        select(func.count()).select_from(r).where(r.error_code.in_(BUDGET_ERROR_CODES)), r.created_at, start, end,
    )).scalar_one())
    by_reason = session.execute(_window(
        select(a.error_kind, func.count()).where(refused).group_by(a.error_kind).order_by(func.count().desc())
        .limit(MAX_GROUPS), a.created_at, start, end)).all()
    by_provider = session.execute(_window(
        select(a.provider, func.count()).where(refused).group_by(a.provider).order_by(func.count().desc())
        .limit(MAX_GROUPS), a.created_at, start, end)).all()
    rows = session.execute(_window(
        select(a.created_at, a.error_kind, a.model_id, a.provider, a.attempt_number, r.status, r.error_code,
               r.mock_mode)
        .join(r, a.request_log_id == r.id).where(refused)
        .order_by(a.created_at.desc(), a.id.desc()).offset(offset).limit(limit),
        a.created_at, start, end)).all()
    events = [
        {"time": t, "reason": kind, "model": model, "provider": provider, "attempt_number": n, "status": status,
         "error_code": code, "mock_mode": mock}
        for t, kind, model, provider, n, status, code, mock in rows
    ]
    return RefusalResult(total, refused_requests, [tuple(x) for x in by_reason], [tuple(x) for x in by_provider],
                         events)
