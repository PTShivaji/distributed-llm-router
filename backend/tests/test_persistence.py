"""PostgreSQL persistence (isolated database llm_router_test) and analytics."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.analytics import summarize
from app.db.migrations import MIGRATIONS, run_migrations
from app.db.models import ProviderAttempt, RequestLog, SchemaMigration
from app.db.recorder import NullRecorder, RequestRecord, RequestRecorder
from app.infra.database import Database
from app.routing.errors import AllModelsFailedError
from app.routing.service import ChatCommand
from tests.conftest import TEST_PG_DB, model

TEST_URL = f"postgresql+psycopg://router:router_password@127.0.0.1:5433/{TEST_PG_DB}"


def attempt(n=1, model_id="m", provider="mock", outcome="success", kind=None):
    return {"attempt_number": n, "model_id": model_id, "provider": provider, "outcome": outcome,
            "error_kind": kind, "retryable": None, "latency_ms": 1.0}


async def with_db(fn):
    db = Database(TEST_URL)
    assert await db.start()
    rec = RequestRecorder(db)
    await rec.start()
    try:
        return await fn(db, rec)
    finally:
        await rec.stop()
        await db.close()


def test_migrations_are_idempotent(pg_clean):
    db = Database(TEST_URL)
    asyncio.run(db.start())
    try:
        assert run_migrations(db.engine) == []  # already applied by start()
        versions = asyncio.run(db.run(lambda s: list(s.execute(select(SchemaMigration.version)).scalars())))
        assert versions == [m.version for m in MIGRATIONS]
    finally:
        asyncio.run(db.close())


def test_concurrent_migration_runs_are_serialised(pg_clean):
    db = Database(TEST_URL)
    asyncio.run(db.start())
    errors: list[BaseException] = []

    def runner():
        try:
            run_migrations(db.engine)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=runner) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    asyncio.run(db.close())
    assert errors == []


def test_recorder_writes_request_and_attempts(pg_clean):
    async def go(db, rec):
        rec.record(RequestRecord(
            request_id="req-1", status="success", query_chars=24, latency_ms=12.5, complexity="SIMPLE",
            selected_model="fast", model_used="backup", provider="mock", fallback_used=True,
            cache_status="miss", input_tokens=6, output_tokens=50, total_tokens=56, usage_source="estimated",
            input_cost=6e-7, output_cost=2e-5, total_cost=2.06e-5, pricing_is_placeholder=True,
            attempts=[attempt(1, "fast", outcome="error", kind="timeout"), attempt(2, "backup")],
        ))
        await rec.flush()

        def check(s):
            row = s.execute(select(RequestLog)).scalar_one()
            attempts = s.execute(select(ProviderAttempt).order_by(ProviderAttempt.attempt_number)).scalars().all()
            return row, attempts

        return await db.run(check), rec

    (row, attempts), rec = asyncio.run(with_db(go))
    assert rec.written == 1 and rec.failed == 0
    assert row.request_id == "req-1" and row.attempt_count == 2 and row.fallback_used
    assert float(row.total_cost) == pytest.approx(2.06e-5)
    assert row.created_at.tzinfo is not None and row.created_at.utcoffset() == timedelta(0)
    assert row.prompt_text is None and row.response_text is None
    assert [(a.model_id, a.outcome, a.error_kind) for a in attempts] == [("fast", "error", "timeout"), ("backup", "success", None)]


def _service_with_db(make_service, rec, *, failures=None, **kw):
    svc = make_service([model("fast", fallbacks=("backup",)), model("backup", priority=500)], failures=failures, **kw)
    svc.recorder = rec
    return svc


def test_chat_success_persisted_without_raw_text(pg_clean, make_service):
    async def go(db, rec):
        svc = _service_with_db(make_service, rec)
        await svc.chat(ChatCommand("My secret is hunter2. What is the capital of France?", "r-ok", client_id="1.2.3.4"))
        await rec.flush()
        return await db.run(lambda s: s.execute(select(RequestLog)).scalar_one())

    row = asyncio.run(with_db(go))
    assert row.status == "success" and row.model_used == "fast" and row.complexity == "SIMPLE"
    assert row.prompt_text is None and row.response_text is None
    assert row.client_hash and "1.2.3.4" not in row.client_hash
    assert row.query_chars == len("My secret is hunter2. What is the capital of France?")


def test_prompt_and_response_stored_only_when_enabled(pg_clean, make_service):
    async def go(db, rec):
        rec.persist_prompts = rec.persist_responses = True
        svc = _service_with_db(make_service, rec)
        out = await svc.chat(ChatCommand("What is the capital of France?", "r-text"))
        await rec.flush()
        return out, await db.run(lambda s: s.execute(select(RequestLog)).scalar_one())

    out, row = asyncio.run(with_db(go))
    assert row.prompt_text == "What is the capital of France?" and row.response_text == out.answer


def test_failed_request_persisted_with_attempts(pg_clean, make_service):
    async def go(db, rec):
        svc = _service_with_db(make_service, rec, failures={"fast": "unavailable", "backup": "unavailable"}, max_retries=0)
        with pytest.raises(AllModelsFailedError):
            await svc.chat(ChatCommand("What is the capital of France?", "r-fail"))
        await rec.flush()
        return await db.run(lambda s: (
            s.execute(select(RequestLog)).scalar_one(),
            s.execute(select(func.count()).select_from(ProviderAttempt)).scalar_one(),
        ))

    row, n_attempts = asyncio.run(with_db(go))
    assert row.status == "error" and row.error_code == "all_models_failed" and row.model_used is None
    assert row.attempt_count == n_attempts == 2


def test_recorder_drops_safely_when_database_down():
    async def go():
        db = Database("postgresql+psycopg://router:x@localhost:1/none", connect_timeout=1)
        assert await db.start() is False
        rec = RequestRecorder(db)
        await rec.start()
        rec.record(RequestRecord(request_id="x", status="success", query_chars=1, latency_ms=1))
        await rec.flush()
        await rec.stop()
        await db.close()
        return rec

    rec = asyncio.run(go())
    assert rec.dropped == 1 and rec.written == 0


def test_null_recorder_is_noop():
    assert NullRecorder().record(RequestRecord(request_id="x", status="success", query_chars=1, latency_ms=1)) is None


def test_analytics_aggregation_and_time_filter(pg_clean):
    now = datetime.now(UTC)
    old = now - timedelta(days=10)

    def rec(**kw):
        base = dict(request_id="r", status="success", query_chars=10, latency_ms=10.0, provider="mock",
                    cache_status="miss", input_tokens=10, output_tokens=20, total_tokens=30, total_cost=0.001)
        base.update(kw)
        return RequestRecord(**base)

    async def go(db, rec_):
        for r in [
            rec(complexity="SIMPLE", model_used="fast", latency_ms=10, attempts=[attempt(model_id="fast")]),
            rec(complexity="SIMPLE", model_used="fast", latency_ms=20, cache_status="hit_exact", total_tokens=0,
                input_tokens=0, output_tokens=0, total_cost=0),
            rec(complexity="COMPLEX", model_used="balanced", latency_ms=30, fallback_used=True,
                attempts=[attempt(1, "advanced", outcome="error", kind="timeout"), attempt(2, "balanced")]),
            rec(status="error", complexity="MEDIUM", model_used=None, provider=None, latency_ms=40, total_tokens=None,
                input_tokens=None, output_tokens=None, total_cost=None,
                attempts=[attempt(1, "m", outcome="error", kind="unavailable"), attempt(2, "m2", outcome="skipped_unhealthy")]),
            rec(complexity="SIMPLE", model_used="fast", created_at=old, latency_ms=1000),
        ]:
            rec_.record(r)
        await rec_.flush()
        all_time = await db.run(lambda s: summarize(s, None, None))
        recent = await db.run(lambda s: summarize(s, now - timedelta(days=1), None))
        return all_time, recent

    all_time, s = asyncio.run(with_db(go))
    assert all_time.total_requests == 5
    assert s.total_requests == 4 and s.successful_requests == 3 and s.failed_requests == 1
    assert s.total_tokens == 60 and s.estimated_total_cost == pytest.approx(0.002)
    assert s.average_latency_ms == pytest.approx(25.0)
    assert (s.cache_hits, s.cache_misses, s.cache_hit_rate) == (1, 3, 0.25)
    assert s.fallback_requests == 1 and s.fallback_rate == 0.25
    assert s.requests_by_complexity == {"SIMPLE": 2, "COMPLEX": 1, "MEDIUM": 1}
    assert [(m["model"], m["requests"]) for m in s.requests_by_model] == [("fast", 2), ("balanced", 1)]
    assert s.provider_attempts == [{"provider": "mock", "successes": 2, "failures": 2, "skipped": 1}]
