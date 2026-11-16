"""Step 10.4 phase 2: read-only budget endpoints (GET /api/v1/budget/status|spend|refusals).

Isolation: Settings(_env_file=None, ...) everywhere (the developer's .env is never read); Redis tests use
logical DB 15 with a unique key prefix (conftest.redis_prefix); PostgreSQL tests use the separate
"llm_router_test" database, truncated by conftest.pg_clean. Nothing calls a real provider.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta

import pytest
import redis as redis_sync
from fastapi.testclient import TestClient

from app.budget.guard import MICROS_PER_USD, BudgetGuard, BudgetLimits, LocalBudgetStore, RedisBudgetStore
from app.core.budget_view import build_budget_status
from app.core.config import Settings
from app.db.recorder import RequestRecord
from app.main import create_app
from app.observability.metrics import Metrics
from tests.conftest import TEST_HOST, TEST_PG_DB, TEST_REDIS_DB, redis_manager
from tests.test_persistence import attempt, with_db
from tests.test_production_hardening import dev_settings, prod_settings, strong
from tests.test_settings_api import SENTINEL_CHAT, SENTINEL_INFRA, SENTINEL_SECRETS, every_sentinel, sentinel_settings

STATUS, SPEND, REFUSALS = "/api/v1/budget/status", "/api/v1/budget/spend", "/api/v1/budget/refusals"
PATHS = (STATUS, SPEND, REFUSALS)
TODAY = lambda: datetime.now(UTC).date()  # noqa: E731

STATUS_KEYS = {
    "period": {"timezone", "granularity", "day", "starts_at", "resets_at"},
    "enforcement": {"configured", "daily_tracking", "applies_to", "currently_applicable", "mock_mode",
                    "real_provider_calls_enabled", "paid_embeddings_enabled", "fail_closed", "store", "store_scope"},
    "limits": {"daily_usd", "daily_usd_per_client", "daily_tokens_per_client", "max_cost_per_request_usd"},
    "today": {"available", "source", "counted_usd", "remaining_usd", "unavailable_reason",
              "per_client_counters_exposed"},
    "monthly": {"enforcement_supported", "limit_usd", "note"},
    "embedding": {"provider", "paid", "budgeted", "persisted_history", "process_counters_available",
                  "process_estimated_cost_usd", "process_events"},
}
FIGURES = {"requests", "successful_requests", "total_tokens", "mock_requests", "real_requests",
           "mock_estimated_cost_usd", "real_estimated_cost_usd", "placeholder_priced_requests", "cost_unknown_requests"}
GROUP = {"name", "requests", "total_tokens", "mock_estimated_cost_usd", "real_estimated_cost_usd",
         "placeholder_priced_requests"}
SPEND_KEYS = {"start_date", "end_date", "timezone", "source", "currency", "cost_basis", "totals", "days",
              "by_provider", "by_provider_truncated", "by_model", "by_model_truncated", "embedding_spend_included",
              "notes"}
REFUSAL_KEYS = {"start_date", "end_date", "timezone", "source", "persisted_scope", "total_refused_attempts",
                "refused_requests", "by_reason", "by_provider", "events", "pagination",
                "embedding_refusals_persisted", "process_counters_available", "process_chat_refusals",
                "process_embedding_refusals", "notes"}
EVENT_KEYS = {"time", "reason", "category", "description", "model", "provider", "attempt_number", "request_outcome",
              "mock_mode"}


def auth(s: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {s.observability_api_token.get_secret_value()}"} if s.observability_api_token else {}


def db_settings(**overrides) -> Settings:
    """Mock-mode settings that read the ISOLATED test database (never the development one)."""
    base = dict(database_enabled=True, postgres_host=TEST_HOST, postgres_db=TEST_PG_DB)
    base.update(overrides)
    return dev_settings(**base)


def guard_for(app) -> BudgetGuard | None:
    return app.state.container.chat_service.budget


def at(day: date, h=12, m=0, s=0, us=0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, us, tzinfo=UTC)


def seed(records: list[RequestRecord]) -> None:
    async def go(db, rec):
        for r in records:
            rec.record(r)
        await rec.flush()
    asyncio.run(with_db(go))


def row(**kw) -> RequestRecord:
    base = dict(request_id="req-id-SENTINEL-0001", status="success", query_chars=10, latency_ms=5.0,
                client_hash="clienthash-SENTINEL", prompt_text="PROMPT-SENTINEL", response_text="ANSWER-SENTINEL",
                cache_status="miss", mock_mode=True, provider="mock", model_used="mock-fast",
                input_tokens=10, output_tokens=20, total_tokens=30, total_cost=0.001, pricing_is_placeholder=True)
    base.update(kw)
    return RequestRecord(**base)


# =========================================================================== #
# 1. Authentication + read-only
# =========================================================================== #
@pytest.mark.parametrize("path", PATHS)
def test_requires_observability_token_when_configured(path):
    s = dev_settings(observability_api_token=strong())
    token = s.observability_api_token.get_secret_value()
    with TestClient(create_app(s)) as c:
        missing = c.get(path)
        wrong = c.get(path, headers={"Authorization": "Bearer nope"})
        in_query = c.get(f"{path}?token={token}")
        ok = c.get(path, headers=auth(s))
    for r in (missing, wrong, in_query):
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
        assert r.headers["WWW-Authenticate"] == "Bearer"
    assert ok.status_code in (200, 503)  # 503 = history endpoints without a database: auth passed
    assert token not in ok.text + missing.text


@pytest.mark.parametrize("path", PATHS)
def test_admin_token_and_chat_key_are_rejected(path):
    s = sentinel_settings()
    with TestClient(create_app(s)) as c:
        for credential in (SENTINEL_SECRETS["admin_api_token"], SENTINEL_CHAT["sentinel-client-alpha"]):
            assert c.get(path, headers={"Authorization": f"Bearer {credential}"}).status_code == 401


def test_protected_in_production():
    s = prod_settings()
    with TestClient(create_app(s)) as c:
        assert {p: c.get(p).status_code for p in PATHS} == {p: 401 for p in PATHS}
        assert c.get(STATUS, headers=auth(s)).status_code == 200


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_read_only(path, method):
    with TestClient(create_app(dev_settings())) as c:
        assert getattr(c, method)(path).status_code == 405


def test_listed_in_openapi_as_get_only():
    with TestClient(create_app(dev_settings())) as c:
        paths = c.get("/openapi.json").json()["paths"]
    for p in PATHS:
        assert set(paths[p]) == {"get"}


# =========================================================================== #
# 2. Allow-list + secret leakage
# =========================================================================== #
def test_status_matches_allow_list_and_leaks_nothing():
    s = sentinel_settings(budget_daily_usd=5.0, budget_daily_usd_per_client=1.0)
    with TestClient(create_app(s)) as c:
        r = c.get(STATUS, headers=auth(s))
        errors = [c.get(p, headers=auth(s)) for p in (SPEND, REFUSALS)]
    body = r.json()
    assert set(body) == set(STATUS_KEYS) | {"notes"}
    for section, keys in STATUS_KEYS.items():
        assert set(body[section]) == keys, section
    exposed = r.text + "".join(e.text for e in errors) + str(dict(r.headers))
    for value in every_sentinel():
        assert value not in exposed
    assert body["today"]["per_client_counters_exposed"] is False


# =========================================================================== #
# 3, 8, 14. Limits, missing budgets, no monthly enforcement (in-memory store, no Redis)
# =========================================================================== #
def test_no_budget_configured_is_null_not_zero():
    with TestClient(create_app(dev_settings())) as c:
        body = c.get(STATUS).json()
    assert body["enforcement"]["configured"] is False and body["enforcement"]["store"] is None
    assert body["enforcement"]["store_scope"] is None
    assert body["limits"] == {"daily_usd": None, "daily_usd_per_client": None, "daily_tokens_per_client": None,
                              "max_cost_per_request_usd": None}
    assert body["today"]["available"] is False
    assert body["today"]["counted_usd"] is None and body["today"]["remaining_usd"] is None
    assert "not tracked" in body["today"]["unavailable_reason"]
    assert body["monthly"]["enforcement_supported"] is False and body["monthly"]["limit_usd"] is None


def test_per_request_limit_only_does_not_track_daily_spend():
    with TestClient(create_app(dev_settings(budget_max_cost_per_request_usd=0.5))) as c:
        body = c.get(STATUS).json()
    assert body["enforcement"]["configured"] is True and body["enforcement"]["daily_tracking"] is False
    assert body["limits"]["max_cost_per_request_usd"] == 0.5 and body["limits"]["daily_usd"] is None
    assert body["today"]["counted_usd"] is None and "per-request" in body["today"]["unavailable_reason"]


def test_counted_and_remaining_from_the_guard_counters_without_mutating_them():
    s = dev_settings(budget_daily_usd=5.0, budget_daily_usd_per_client=2.0, budget_daily_tokens_per_client=10_000)
    app = create_app(s)
    with TestClient(app) as c:
        guard = guard_for(app)
        assert isinstance(guard.store, LocalBudgetStore)
        assert c.get(STATUS).json()["today"]["counted_usd"] == 0.0  # tracked, reachable, nothing spent: a real 0
        decision = asyncio.run(guard.reserve_cost(1.25, 100, "client-a", operation="embedding"))
        assert decision.allowed
        before = dict(guard.store._counters)
        bodies = [c.get(STATUS).json() for _ in range(3)]
        after = dict(guard.store._counters)
    assert before == after  # read-only
    body = bodies[-1]
    assert body["today"] == {"available": True, "source": "budget_counters", "counted_usd": 1.25,
                             "remaining_usd": 3.75, "unavailable_reason": None, "per_client_counters_exposed": False}
    assert body["limits"] == {"daily_usd": 5.0, "daily_usd_per_client": 2.0, "daily_tokens_per_client": 10_000,
                              "max_cost_per_request_usd": None}
    assert body["enforcement"]["store"] == "memory"
    assert body["enforcement"]["store_scope"] == "this_process_only"
    assert any("process-local" in n for n in body["notes"])
    assert "client-a" not in str(bodies)


def test_remaining_never_negative_and_only_with_a_global_limit():
    app = create_app(dev_settings(budget_daily_usd=1.0))
    with TestClient(app) as c:
        guard = guard_for(app)
        day, _ = guard.period()
        asyncio.run(guard.store.adjust(day, "x", 3 * MICROS_PER_USD, 0))  # e.g. a kept reservation above the limit
        assert c.get(STATUS).json()["today"]["remaining_usd"] == 0.0
    app = create_app(dev_settings(budget_daily_usd_per_client=1.0))  # per-client only: no global remaining
    with TestClient(app) as c:
        body = c.get(STATUS).json()
    assert body["today"]["counted_usd"] == 0.0 and body["today"]["remaining_usd"] is None


# =========================================================================== #
# 4. Daily reset boundary (UTC), one clock read for period + counters
# =========================================================================== #
def test_daily_reset_boundary_is_utc_midnight():
    clock = [datetime(2026, 11, 11, 23, 59, 59, 500_000, tzinfo=UTC).timestamp()]
    store = LocalBudgetStore()
    guard = BudgetGuard(store, BudgetLimits(daily_usd=10.0), clock=lambda: clock[0])
    s = dev_settings(budget_daily_usd=10.0)
    assert asyncio.run(guard.reserve_cost(2.0, 1, "c")).allowed

    late = asyncio.run(build_budget_status(s, guard, None))
    assert str(late.period.day) == "2026-11-11"
    assert late.period.starts_at == datetime(2026, 11, 11, tzinfo=UTC)
    assert late.period.resets_at == datetime(2026, 11, 12, tzinfo=UTC)
    assert late.today.counted_usd == 2.0 and late.today.remaining_usd == 8.0

    clock[0] = datetime(2026, 11, 12, 0, 0, 0, tzinfo=UTC).timestamp()
    fresh = asyncio.run(build_budget_status(s, guard, None))
    assert str(fresh.period.day) == "2026-11-12"
    assert fresh.today.counted_usd == 0.0 and fresh.today.remaining_usd == 10.0


# =========================================================================== #
# 6-7. Mock vs real applicability (unit; nothing is called)
# =========================================================================== #
def test_mock_mode_limits_are_configured_but_not_applicable():
    body = asyncio.run(build_budget_status(dev_settings(budget_daily_usd=5.0), None, None))
    assert body.enforcement.mock_mode is True and body.enforcement.currently_applicable is False


def test_real_calls_make_limits_applicable():
    s = Settings(_env_file=None, environment="test", llm_mock_mode=False, allow_real_provider_calls=True,
                 budget_daily_usd=5.0, redis_enabled=False, database_enabled=False)
    guard = BudgetGuard(LocalBudgetStore(), BudgetLimits(daily_usd=5.0))
    body = asyncio.run(build_budget_status(s, guard, None))
    assert body.enforcement.real_provider_calls_enabled is True
    assert body.enforcement.currently_applicable is True
    assert body.today.counted_usd == 0.0 and body.today.remaining_usd == 5.0


# =========================================================================== #
# 9. Redis: real counters (DB 15, isolated prefix) and Redis unreachable
# =========================================================================== #
def test_redis_counters_are_reported_and_never_mutated(infra_settings, redis_prefix):
    s = infra_settings(budget_daily_usd=5.0, budget_daily_usd_per_client=1.0, cache_enabled=False,
                       rate_limit_enabled=False, health_backend="memory")
    app = create_app(s)
    with TestClient(app) as c:
        guard = guard_for(app)
        assert isinstance(guard.store, RedisBudgetStore)
        day, _ = guard.period()

        async def reserve():  # same store code as the enforcement path, separate connection
            manager = redis_manager(redis_prefix)
            await manager.start()
            try:
                return await RedisBudgetStore(manager).reserve(day, "client-x", 750_000, 42, (5_000_000, 1_000_000, 0))
            finally:
                await manager.close()

        assert asyncio.run(reserve()) == 0
        raw = redis_sync.Redis(host=TEST_HOST, port=6379, db=TEST_REDIS_DB)
        keys = sorted(raw.scan_iter(match=f"{redis_prefix}budget*"))
        assert len(keys) == 3
        snap = lambda: [(raw.get(k), raw.pttl(k)) for k in keys]  # noqa: E731
        before = snap()
        bodies = [c.get(STATUS).json() for _ in range(3)]
        after = snap()
        if guard.period()[0] != day:
            pytest.skip("crossed UTC midnight during the test")
    assert [v for v, _ in before] == [v for v, _ in after]
    assert all(a_ttl <= b_ttl for (_, b_ttl), (_, a_ttl) in zip(before, after, strict=True))  # TTL never refreshed
    body = bodies[-1]
    assert body["today"]["counted_usd"] == 0.75 and body["today"]["remaining_usd"] == 4.25
    assert body["enforcement"]["store"] == "redis"
    assert body["enforcement"]["store_scope"] == "shared_across_instances"
    assert "client-x" not in str(bodies)


def test_redis_unreachable_reports_unknown_not_zero():
    s = dev_settings(redis_enabled=True, redis_host=TEST_HOST, redis_port=1, redis_db=15, budget_daily_usd=5.0,
                     redis_connect_timeout_seconds=0.2, redis_socket_timeout_seconds=0.2)
    with TestClient(create_app(s)) as c:
        r = c.get(STATUS)
    body = r.json()
    assert r.status_code == 200
    assert body["enforcement"]["store"] == "redis"
    assert body["today"]["available"] is False
    assert body["today"]["counted_usd"] is None and body["today"]["remaining_usd"] is None
    assert "unreachable" in body["today"]["unavailable_reason"]
    assert body["limits"]["daily_usd"] == 5.0  # configuration is still reported


# =========================================================================== #
# 13. Embedding visibility (process counters only, clearly labelled)
# =========================================================================== #
def test_embedding_process_counters_and_unavailable_state():
    metrics = Metrics(enabled=True)
    body = asyncio.run(build_budget_status(dev_settings(), None, metrics))
    assert body.embedding.provider == "local" and body.embedding.paid is False and body.embedding.budgeted is False
    assert body.embedding.persisted_history is False
    assert body.embedding.process_counters_available is True
    assert body.embedding.process_estimated_cost_usd == 0.0 and body.embedding.process_events == []

    metrics.embedding_cost(0.002)
    metrics.embedding_budget("refused", "global_daily_budget")
    body = asyncio.run(build_budget_status(dev_settings(), None, metrics))
    assert body.embedding.process_estimated_cost_usd == pytest.approx(0.002)
    assert [(g.labels, g.value) for g in body.embedding.process_events] == [
        ({"event": "refused", "reason": "global_daily_budget"}, 1.0)]

    disabled = asyncio.run(build_budget_status(dev_settings(), None, Metrics(enabled=False)))
    assert disabled.embedding.process_counters_available is False
    assert disabled.embedding.process_estimated_cost_usd is None and disabled.embedding.process_events is None


def test_metrics_disabled_via_settings():
    with TestClient(create_app(dev_settings(metrics_enabled=False))) as c:
        body = c.get(STATUS).json()
    assert body["embedding"]["process_counters_available"] is False
    assert body["embedding"]["process_estimated_cost_usd"] is None


# =========================================================================== #
# 12. Date range + pagination validation (no database needed: validated first)
# =========================================================================== #
@pytest.mark.parametrize("path", [SPEND, REFUSALS])
def test_date_range_validation(path):
    today = TODAY()
    with TestClient(create_app(dev_settings())) as c:
        reversed_ = c.get(path, params={"start_date": str(today), "end_date": str(today - timedelta(days=1))})
        too_long = c.get(path, params={"start_date": str(today - timedelta(days=92)), "end_date": str(today)})
        future = c.get(path, params={"end_date": str(today + timedelta(days=1))})
        malformed = c.get(path, params={"start_date": "2026-13-40"})
        max_ok = c.get(path, params={"start_date": str(today - timedelta(days=91)), "end_date": str(today)})
    for r in (reversed_, too_long, future):
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_time_range"
    assert malformed.status_code == 422
    assert max_ok.status_code == 503  # valid range; only the (disabled) database is missing


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}, {"page": "x"}])
def test_pagination_validation(params):
    with TestClient(create_app(dev_settings())) as c:
        assert c.get(REFUSALS, params=params).status_code == 422


# =========================================================================== #
# 10. PostgreSQL disabled / unreachable
# =========================================================================== #
def test_database_disabled_or_unreachable_is_transparent():
    for s in (dev_settings(), db_settings(postgres_port=1, database_connect_timeout_seconds=1)):
        with TestClient(create_app(s)) as c:
            for path in (SPEND, REFUSALS):
                r = c.get(path)
                assert r.status_code == 503 and r.json()["error"]["code"] == "database_unavailable"
            assert c.get(STATUS).status_code == 200  # status does not depend on the database


# =========================================================================== #
# 16. Cache-Control: no-store
# =========================================================================== #
def test_no_store_on_status_even_without_security_headers():
    for s in (dev_settings(), dev_settings(security_headers_enabled=False)):
        with TestClient(create_app(s)) as c:
            assert c.get(STATUS).headers["Cache-Control"] == "no-store"


# =========================================================================== #
# 5-7, 11, 15-16. Persisted history (isolated llm_router_test database)
# =========================================================================== #
def test_spend_by_day_provider_model_with_mock_and_real_split(pg_clean):
    today = TODAY()
    d1, d2 = today - timedelta(days=2), today - timedelta(days=1)
    seed([
        # day 1: two mock rows (placeholder prices), one at the very end of the UTC day
        row(created_at=at(d1, 9), model_used="mock-fast", total_cost=0.001),
        row(created_at=at(d1, 23, 59, 59, 999_000), model_used="mock-advanced", total_tokens=70, total_cost=0.004),
        # day 2: starts exactly at UTC midnight; real-call estimates (configured prices) for two providers
        row(created_at=at(d2, 0, 0, 0), mock_mode=False, provider="openai", model_used="gpt-x", total_tokens=100,
            total_cost=0.02, pricing_is_placeholder=False),
        row(created_at=at(d2, 10), mock_mode=False, provider="anthropic", model_used="claude-x", total_tokens=50,
            total_cost=0.03, pricing_is_placeholder=False),
        row(created_at=at(d2, 11), mock_mode=False, provider="openai", model_used="gpt-x", total_tokens=10,
            total_cost=None, pricing_is_placeholder=None, usage_source="unknown"),  # cost not recorded
        row(created_at=at(d2, 12), status="error", error_code="all_models_failed", provider=None, model_used=None,
            total_tokens=None, total_cost=None, pricing_is_placeholder=None),
        # outside the requested range
        row(created_at=at(d1 - timedelta(days=1), 23, 59, 59), total_cost=9.0),
    ])
    s = db_settings(observability_api_token=strong())
    with TestClient(create_app(s)) as c:
        r = c.get(SPEND, headers=auth(s), params={"start_date": str(d1), "end_date": str(today)})
        default = c.get(SPEND, headers=auth(s))
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    body = r.json()
    assert set(body) == SPEND_KEYS and set(body["totals"]) == FIGURES
    assert all(set(d) == FIGURES | {"date"} for d in body["days"])
    assert all(set(g) == GROUP for g in body["by_provider"] + body["by_model"])
    days = {d["date"]: d for d in body["days"]}
    assert list(days) == [str(d1), str(d2), str(today)]  # zero-filled, inclusive, UTC
    assert days[str(d1)]["requests"] == 2 and days[str(d1)]["mock_estimated_cost_usd"] == pytest.approx(0.005)
    assert days[str(d1)]["real_estimated_cost_usd"] == 0 and days[str(d1)]["placeholder_priced_requests"] == 2
    assert days[str(d2)]["requests"] == 4 and days[str(d2)]["successful_requests"] == 3
    assert days[str(d2)]["real_estimated_cost_usd"] == pytest.approx(0.05)
    assert days[str(d2)]["cost_unknown_requests"] == 1
    assert (days[str(d2)]["real_requests"], days[str(d2)]["mock_requests"]) == (3, 1)  # the error row is a mock row
    assert days[str(today)]["requests"] == 0
    t = body["totals"]
    assert (t["requests"], t["mock_requests"], t["real_requests"]) == (6, 3, 3)
    assert t["mock_estimated_cost_usd"] == pytest.approx(0.005) and t["real_estimated_cost_usd"] == pytest.approx(0.05)
    providers = {g["name"]: g for g in body["by_provider"]}
    assert set(providers) == {"mock", "openai", "anthropic"}  # error row has no provider
    assert providers["openai"]["requests"] == 2 and providers["openai"]["real_estimated_cost_usd"] == pytest.approx(0.02)
    assert providers["mock"]["mock_estimated_cost_usd"] == pytest.approx(0.005)
    models = {g["name"]: g for g in body["by_model"]}
    assert set(models) == {"mock-fast", "mock-advanced", "gpt-x", "claude-x"}
    assert body["cost_basis"].startswith("mixed") and "not invoices" in body["cost_basis"]
    assert body["embedding_spend_included"] is False
    assert any("never provider invoices" in n for n in body["notes"])
    for secret in ("PROMPT-SENTINEL", "ANSWER-SENTINEL", "clienthash-SENTINEL", "req-id-SENTINEL"):
        assert secret not in r.text
    d = default.json()
    assert d["end_date"] == str(today) and len(d["days"]) == 30


def test_mock_only_history_is_labelled_simulated(pg_clean):
    seed([row(created_at=at(TODAY(), 0, 0, 1))])
    with TestClient(create_app(db_settings())) as c:
        body = c.get(SPEND).json()
    assert body["cost_basis"].startswith("mock mode only") and "no money" in body["cost_basis"]
    assert body["totals"]["real_estimated_cost_usd"] == 0 and body["totals"]["placeholder_priced_requests"] == 1


def test_refusals_events_grouping_pagination_and_safety(pg_clean):
    today = TODAY()
    seed([
        # refused request: every tried model skipped by the budget
        row(created_at=at(today, 0, 0, 5), status="error", error_code="budget_exceeded", provider=None,
            model_used=None, total_cost=None, attempts=[
                attempt(1, "gpt-x", "openai", "skipped_budget", "global_daily_budget"),
                attempt(2, "claude-x", "anthropic", "skipped_budget", "client_daily_budget")]),
        # served by a fallback after a per-request refusal
        row(created_at=at(today, 0, 0, 6), mock_mode=False, provider="mock", model_used="mock-fast", attempts=[
            attempt(1, "gpt-x", "openai", "skipped_budget", "per_request_limit"),
            attempt(2, "mock-fast", "mock", "success")]),
        # store unreachable (fail closed) and an unrecognised reason
        row(created_at=at(today, 0, 0, 7), status="error", error_code="budget_unavailable", provider=None,
            model_used=None, total_cost=None, attempts=[
                attempt(1, "gpt-x", "openai", "skipped_budget", "budget_unavailable"),
                attempt(2, "claude-x", "anthropic", "skipped_budget", "weird-kind")]),
        # NOT a budget refusal
        row(created_at=at(today, 0, 0, 8), status="error", error_code="all_models_failed", provider=None,
            model_used=None, attempts=[attempt(1, "gpt-x", "openai", "error", "timeout")]),
        # outside the default 7-day window
        row(created_at=at(today - timedelta(days=8)), status="error", error_code="budget_exceeded", attempts=[
            attempt(1, "gpt-x", "openai", "skipped_budget", "global_daily_budget")]),
    ])
    s = db_settings(observability_api_token=strong())
    with TestClient(create_app(s)) as c:
        full = c.get(REFUSALS, headers=auth(s))
        p1 = c.get(REFUSALS, headers=auth(s), params={"page": 1, "page_size": 2}).json()
        p3 = c.get(REFUSALS, headers=auth(s), params={"page": 3, "page_size": 2}).json()
        p9 = c.get(REFUSALS, headers=auth(s), params={"page": 9, "page_size": 2}).json()
    assert full.status_code == 200 and full.headers["Cache-Control"] == "no-store"
    body = full.json()
    assert set(body) == REFUSAL_KEYS and all(set(e) == EVENT_KEYS for e in body["events"])
    assert body["total_refused_attempts"] == 5 and body["refused_requests"] == 2
    assert {x["key"]: x["count"] for x in body["by_reason"]} == {
        "global_daily_budget": 1, "client_daily_budget": 1, "per_request_limit": 1, "budget_unavailable": 1,
        "other": 1}
    assert {x["key"]: x["count"] for x in body["by_provider"]} == {"openai": 3, "anthropic": 2}
    events = body["events"]
    assert [e["time"] for e in events] == sorted((e["time"] for e in events), reverse=True)
    outcomes = {(e["reason"], e["request_outcome"]) for e in events}
    assert ("per_request_limit", "served_by_another_model") in outcomes
    assert ("global_daily_budget", "refused") in outcomes and ("budget_unavailable", "refused") in outcomes
    other = next(e for e in events if e["reason"] == "other")
    assert other["category"] == "other" and "weird-kind" not in full.text
    assert next(e for e in events if e["reason"] == "budget_unavailable")["category"] == "store_unavailable"
    assert body["embedding_refusals_persisted"] is False
    assert any("not persisted" in n for n in body["notes"])
    for secret in ("PROMPT-SENTINEL", "ANSWER-SENTINEL", "clienthash-SENTINEL", "req-id-SENTINEL"):
        assert secret not in full.text
    assert p1["pagination"] == {"page": 1, "page_size": 2, "total": 5, "has_more": True} and len(p1["events"]) == 2
    assert p3["pagination"]["has_more"] is False and len(p3["events"]) == 1
    assert p9["events"] == [] and p9["pagination"]["total"] == 5
    assert [e["time"] for e in p1["events"]] == [e["time"] for e in events[:2]]


def test_no_historical_refusals_is_reported_as_empty_not_invented(pg_clean):
    with TestClient(create_app(db_settings())) as c:
        body = c.get(REFUSALS).json()
        spend = c.get(SPEND).json()
    assert body["total_refused_attempts"] == 0 and body["refused_requests"] == 0
    assert body["events"] == [] and body["by_reason"] == [] and body["by_provider"] == []
    assert body["pagination"] == {"page": 1, "page_size": 50, "total": 0, "has_more": False}
    assert body["process_counters_available"] is True and body["process_chat_refusals"] == []
    assert spend["totals"]["requests"] == 0 and spend["cost_basis"] == "no persisted requests in this range"


def test_read_requests_do_not_write_history(pg_clean):
    import psycopg

    from tests.conftest import _postgres_admin_conninfo

    seed([row(created_at=at(TODAY(), 1))])

    def counts():
        with psycopg.connect(_postgres_admin_conninfo(TEST_PG_DB)) as conn:
            return [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("request_log", "provider_attempt")]

    before = counts()
    with TestClient(create_app(db_settings())) as c:
        for path in PATHS:
            assert c.get(path).status_code == 200
    assert counts() == before


def test_sentinel_infra_values_not_in_history_responses(pg_clean):
    s = db_settings(observability_api_token=strong(), redis_host=SENTINEL_INFRA["redis_host"],
                    tracing_otlp_endpoint=SENTINEL_INFRA["tracing_otlp_endpoint"])
    with TestClient(create_app(s)) as c:
        text = "".join(c.get(p, headers=auth(s)).text for p in PATHS)
    assert TEST_PG_DB not in text and SENTINEL_INFRA["redis_host"] not in text
    assert SENTINEL_INFRA["tracing_otlp_endpoint"] not in text and "postgresql" not in text
