"""Phase 11.2 (H2): PostgreSQL recovery without an application restart.

PostgreSQL outages are simulated with a local TCP proxy in front of the test server
(the container itself is never touched). Every connection goes DIRECTLY to the isolated
database `llm_router_test` (verified with current_database()); these tests do not use the
`postgres_available` fixture and never connect to any other database. Rows are removed
before and after each test. Redis (when an app is built) uses DB 15 with a unique prefix.
"""

import asyncio
import logging
import socket
import threading
import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import app.db.recorder as recorder_module
import app.infra.database as database_module
from app.core.config import Settings
from app.db.models import RequestLog
from app.db.recorder import RequestRecord, RequestRecorder
from app.infra.database import Database
from app.main import create_app
from app.observability.metrics import Metrics
from tests.conftest import TEST_HOST, TEST_PG_DB

assert TEST_PG_DB == "llm_router_test"
_real_sleep = asyncio.sleep  # captured before any test patches asyncio.sleep
FAST = dict(reconnect_initial_seconds=0.05, reconnect_max_seconds=0.2)


def _settings() -> Settings:
    return Settings(_env_file=None)


def _conninfo(port: int) -> str:
    s = _settings()
    return (f"host={TEST_HOST} port={port} user={s.postgres_user} "
            f"password={s.postgres_password.get_secret_value()} dbname={TEST_PG_DB} connect_timeout=2")


def _url(port: int) -> str:
    s = _settings()
    return (f"postgresql+psycopg://{s.postgres_user}:{s.postgres_password.get_secret_value()}"
            f"@{TEST_HOST}:{port}/{TEST_PG_DB}")


def _truncate() -> None:
    with psycopg.connect(_conninfo(_settings().postgres_port), autocommit=True) as conn:
        assert conn.execute("select current_database()").fetchone()[0] == TEST_PG_DB
        exists = conn.execute("SELECT to_regclass('public.request_log')").fetchone()[0]
        if exists:
            conn.execute("TRUNCATE request_log, provider_attempt RESTART IDENTITY CASCADE")


def _rows(request_ids: list[str]) -> dict[str, int]:
    with psycopg.connect(_conninfo(_settings().postgres_port), autocommit=True) as conn:
        assert conn.execute("select current_database()").fetchone()[0] == TEST_PG_DB
        found = conn.execute("select request_id, count(*) from request_log where request_id = any(%s) "
                             "group by request_id", (request_ids,)).fetchall()
    return {rid: n for rid, n in found}


@pytest.fixture
def test_db() -> Iterator[None]:
    """Direct, verified connection to llm_router_test only (no other database is contacted)."""
    try:
        _truncate()
    except psycopg.OperationalError:
        pytest.skip("llm_router_test not reachable on 127.0.0.1:5433 (start it with: docker compose up -d)")
    yield
    _truncate()


class PgProxy:
    """TCP proxy in front of the test PostgreSQL: cut() simulates an outage, restore() ends it."""

    def __init__(self, *, up: bool = True) -> None:
        self.up = up
        self._socks: list[socket.socket] = []
        self._lock = threading.Lock()
        self._server = socket.create_server((TEST_HOST, 0))
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            if not self.up:
                client.close()
                continue
            upstream = socket.create_connection((TEST_HOST, _settings().postgres_port))
            with self._lock:
                self._socks += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def cut(self) -> None:
        self.up = False
        with self._lock:
            socks, self._socks = self._socks, []
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
                s.close()
            except OSError:
                pass

    def restore(self) -> None:
        self.up = True

    def close(self) -> None:
        self.cut()
        self._server.close()


@pytest.fixture
def proxy() -> Iterator[PgProxy]:
    p = PgProxy()
    yield p
    p.close()


def rec(rid: str, **kw) -> RequestRecord:
    return RequestRecord(request_id=rid, status="success", query_chars=5, latency_ms=1.0, **kw)


def metrics() -> Metrics:
    return Metrics(enabled=True, model_ids=[], providers=set(), app_version="t", mock_mode=True)


def value(counter, **labels) -> float:
    return (counter.labels(**labels) if labels else counter)._value.get()


async def until(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await _real_sleep(0.02)


# ------------------------------------------------------------- startup outage -> recovery -> resume
def test_unavailable_at_startup_then_reconnects_and_resumes_persistence(test_db, proxy):
    """Startup outage, later availability, automatic reconnection (no restart), backlog persisted."""
    proxy.cut()
    m = metrics()
    rids = [f"h2-startup-{i}-{uuid.uuid4().hex[:6]}" for i in range(5)]

    async def go():
        db = Database(_url(proxy.port), connect_timeout=1, **FAST)
        db.metrics = m
        recorder = RequestRecorder(db)
        recorder.metrics = m
        assert await db.start() is False and db.recovering  # app starts; reconnect loop running
        await recorder.start()
        try:
            for r in rids:
                recorder.record(rec(r))  # queued, not dropped, while PostgreSQL is down
            await _real_sleep(0.4)
            assert recorder.dropped == 0 and recorder.pending == 5 and recorder.written == 0
            failed_attempts = value(m.db_reconnect_attempts, outcome="failure")
            proxy.restore()  # PostgreSQL comes back
            await until(lambda: db.available)
            await until(lambda: recorder.written == 5)
            return db.recoveries, failed_attempts, recorder.dropped
        finally:
            await recorder.stop()
            await db.close()

    recoveries, failed_attempts, dropped = asyncio.run(go())
    assert recoveries == 1 and failed_attempts >= 1 and dropped == 0
    assert value(m.db_available) == 0  # closed at the end
    assert value(m.db_reconnect_attempts, outcome="success") == 1
    assert value(m.db_persist_records_total, outcome="written") == 5
    assert _rows(rids) == {r: 1 for r in rids}  # every queued record persisted exactly once


# ------------------------------------------------------------- outage during writes
def test_disconnection_during_writes_retries_waits_for_recovery_and_loses_nothing(test_db, proxy):
    m = metrics()
    rids = [f"h2-midwrite-{i}-{uuid.uuid4().hex[:6]}" for i in range(4)]

    async def go():
        db = Database(_url(proxy.port), connect_timeout=1, **FAST)
        db.metrics = m
        recorder = RequestRecorder(db, write_max_attempts=2, retry_backoff_seconds=0.05)
        recorder.metrics = m
        assert await db.start() is True
        await recorder.start()
        try:
            recorder.record(rec(rids[0]))
            await until(lambda: recorder.written == 1)
            proxy.cut()  # PostgreSQL disappears while the app is running
            for r in rids[1:]:
                recorder.record(rec(r))
            await until(lambda: not db.available)  # retries exhausted -> reported unavailable
            assert recorder.retries >= 2 and recorder.failed == 0 and recorder.dropped == 0
            proxy.restore()
            await until(lambda: recorder.written == 4)
            return recorder.retries, db.recoveries
        finally:
            await recorder.stop()
            await db.close()

    retries, recoveries = asyncio.run(go())
    assert retries >= 2 and recoveries == 1
    assert value(m.db_write_retries) == retries
    assert value(m.db_persist_records_total, outcome="written") == 4
    assert _rows(rids) == {r: 1 for r in rids}


# ------------------------------------------------------------- no duplicates after an ambiguous commit
def test_retry_after_lost_commit_acknowledgement_does_not_duplicate(test_db):
    """The first write COMMITS, then fails as if the connection dropped before the reply arrived."""
    rids = [f"h2-dedupe-{i}-{uuid.uuid4().hex[:6]}" for i in range(3)]

    async def go():
        db = Database(_url(_settings().postgres_port), **FAST)
        assert await db.start()
        recorder = RequestRecorder(db, retry_backoff_seconds=0.01)
        real_run, calls = db.run, {"n": 0}

        async def commit_then_lose_ack(fn):
            result = await real_run(fn)
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("COMMIT", {}, ConnectionError("server closed the connection unexpectedly"))
            return result

        db.run = commit_then_lose_ack
        await recorder.start()
        try:
            for r in rids:
                recorder.record(rec(r, attempts=[{"attempt_number": 1, "model_id": "m", "provider": "mock",
                                                  "outcome": "success", "error_kind": None, "retryable": None,
                                                  "latency_ms": 1.0}]))
            await until(lambda: recorder.written + recorder.deduplicated == 3)
            return recorder.written, recorder.deduplicated, recorder.retries
        finally:
            await recorder.stop()
            await db.close()

    written, deduplicated, retries = asyncio.run(go())
    assert retries == 1 and deduplicated == 3 and written == 0  # the retry found all rows already stored
    assert _rows(rids) == {r: 1 for r in rids}  # exactly one row each


# ------------------------------------------------------------- retry limits and backoff (no database needed)
class _FlakyDatabase:
    """Stand-in Database: run() fails `fail` times with a connection error, then succeeds."""

    def __init__(self, fail: int) -> None:
        self.fail, self.calls, self.available, self.marked = fail, 0, True, []

    async def run(self, fn):
        self.calls += 1
        if self.calls <= self.fail:
            raise OperationalError("INSERT", {}, ConnectionError("connection refused"))
        return (1, 0)

    def mark_unavailable(self, reason: str) -> None:
        self.marked.append(reason)

    async def wait_available(self) -> None:
        return None


def test_write_retry_limit_and_exponential_backoff(monkeypatch):
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(seconds):
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(recorder_module.asyncio, "sleep", record_sleep)

    async def go(fail):
        db = _FlakyDatabase(fail)
        recorder = RequestRecorder(db, write_max_attempts=3, retry_backoff_seconds=0.5, retry_backoff_max_seconds=1.5)
        await recorder.start()
        recorder.record(rec("h2-backoff"))
        await until(lambda: recorder.written == 1, timeout=5)
        await recorder.stop()
        return db, recorder

    db, recorder = asyncio.run(go(fail=2))  # fails twice, succeeds on the 3rd try: no hand-off
    assert db.calls == 3 and db.marked == [] and recorder.retries == 2
    assert delays[:2] == [0.5, 1.0]  # exponential backoff between attempts

    delays.clear()
    db, recorder = asyncio.run(go(fail=5))  # 3 failures -> hand-off to the reconnect loop, then 2 more
    assert db.marked == ["write_failed:OperationalError"]  # exactly one hand-off after the limit
    assert db.calls == 6 and recorder.written == 1 and recorder.failed == 0 and recorder.dropped == 0
    assert delays == [0.5, 1.0, 0.5, 1.0]  # backoff restarts after the hand-off
    assert [recorder._backoff(n) for n in (1, 2, 3, 4)] == [0.5, 1.0, 1.5, 1.5]  # capped at the maximum


def test_reconnect_loop_backoff_is_exponential_capped_and_cancellable(monkeypatch):
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(seconds):
        delays.append(round(seconds, 3))
        await real_sleep(0)

    monkeypatch.setattr(database_module.asyncio, "sleep", record_sleep)
    monkeypatch.setattr(database_module.random, "uniform", lambda a, b: 1.0)  # no jitter for the assertion

    async def go():
        db = Database("postgresql+psycopg://user:x@127.0.0.1:1/none", reconnect_initial_seconds=1.0,
                      reconnect_max_seconds=8.0, connect_timeout=1)
        attempts = {"n": 0}

        def failing_connect():
            attempts["n"] += 1
            raise OperationalError("SELECT 1", {}, ConnectionError("refused"))

        db._connect_sync = failing_connect
        assert await db.start() is False
        await until(lambda: attempts["n"] >= 7, timeout=5)
        await db.close()  # cancels the loop
        n = attempts["n"]
        await real_sleep(0.05)
        return n, attempts["n"], db.recovering

    before, after, recovering = asyncio.run(go())
    assert delays[:7] == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0, 8.0]  # doubling, capped at the maximum
    assert after == before and recovering is False  # stopped on close: no attempts after shutdown


# ------------------------------------------------------------- APIs recover without a restart
def _app_settings(infra_settings, port: int, **kw) -> Settings:
    return infra_settings(database_enabled=True, postgres_host=TEST_HOST, postgres_port=port, postgres_db=TEST_PG_DB,
                          database_connect_timeout_seconds=1, database_reconnect_initial_seconds=0.05,
                          database_reconnect_max_seconds=1, rate_limit_enabled=False, **kw)


def test_analytics_and_budget_apis_recover_and_chat_history_resumes(test_db, proxy, infra_settings):
    proxy.cut()
    with TestClient(create_app(_app_settings(infra_settings, proxy.port))) as c:
        container = c.app.state.container
        assert container.database.available is False
        down = [c.get("/api/v1/analytics/summary"), c.get("/api/v1/budget/spend"), c.get("/api/v1/budget/refusals")]
        chat_down = c.post("/api/v1/chat", json={"query": "What is PostgreSQL?"})  # served during the outage
        deps_down = c.get("/health/dependencies").json()

        proxy.restore()
        deadline = time.monotonic() + 15
        while not container.database.available:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        chat_up = c.post("/api/v1/chat", json={"query": "What is Redis?"})
        deadline = time.monotonic() + 15
        while container.recorder.written < 2:  # backlog from the outage + the new request
            assert time.monotonic() < deadline
            time.sleep(0.02)
        up = [c.get("/api/v1/analytics/summary"), c.get("/api/v1/budget/spend"), c.get("/api/v1/budget/refusals")]
        summary = up[0].json()
        deps_up = c.get("/health/dependencies").json()
        gauge = value(container.metrics.db_available)

    assert [r.status_code for r in down] == [503, 503, 503]
    assert all(r.json()["error"]["code"] == "database_unavailable" for r in down)
    assert chat_down.status_code == 200 and chat_up.status_code == 200  # chat never depended on PostgreSQL
    assert deps_down["database"]["reachable"] is False and deps_up["database"]["reachable"] is True
    assert [r.status_code for r in up] == [200, 200, 200]  # recovered without restarting the app
    assert summary["total_requests"] == 2  # the request made DURING the outage was not lost
    assert deps_up["persistence"]["dropped"] == 0 and deps_up["persistence"]["failed"] == 0
    assert gauge == 1


# ------------------------------------------------------------- persistent failure is reported, bounded
def test_persistent_outage_is_bounded_and_reported(proxy, infra_settings, caplog):
    proxy.cut()  # never restored: PostgreSQL stays unreachable for the whole test
    s = _app_settings(infra_settings, proxy.port, persistence_queue_size=3)
    with caplog.at_level(logging.INFO), TestClient(create_app(s)) as c:
        container = c.app.state.container
        for i in range(5):
            assert c.post("/api/v1/chat", json={"query": f"outage question {i}"}).status_code == 200
        api = c.get("/api/v1/analytics/summary")
        time.sleep(0.5)  # several reconnect attempts
        m = container.metrics
        queue_full = value(m.db_persist_records_total, outcome="dropped_queue_full")
        reconnect_failures = value(m.db_reconnect_attempts, outcome="failure")
        recorder = container.recorder
    events = [getattr(r, "event", None) for r in caplog.records]

    assert api.status_code == 503 and api.json()["error"]["code"] == "database_unavailable"
    # Bounded: queue (3) + the one batch the worker holds while waiting = 4 kept; the 5th is refused.
    assert queue_full == 1  # counted and logged, never unbounded memory
    assert recorder.dropped == 5  # the refused one + the 4 still pending at shutdown (database never returned)
    assert value(m.db_persist_records_total, outcome="dropped_shutdown") == 4
    assert reconnect_failures >= 2 and value(m.db_reconnect_attempts, outcome="success") == 0
    assert value(m.db_available) == 0
    assert {"db.unavailable", "db.reconnect_failed", "db.persist_dropped"} <= set(events)


def test_permanent_write_error_is_reported_and_not_retried(test_db):
    """A non-connection error (here: request_id longer than its column) cannot succeed on retry."""
    async def go():
        db = Database(_url(_settings().postgres_port), **FAST)
        assert await db.start()
        recorder = RequestRecorder(db, retry_backoff_seconds=0.01)
        await recorder.start()
        try:
            recorder.record(rec("x" * 200))
            await until(lambda: recorder.failed == 1)
            ok = f"h2-after-permanent-{uuid.uuid4().hex[:6]}"
            recorder.record(rec(ok))  # the worker keeps going
            await until(lambda: recorder.written == 1)
            return recorder.retries, db.available, ok
        finally:
            await recorder.stop()
            await db.close()

    retries, available, ok = asyncio.run(go())
    assert retries == 0 and available is True  # not treated as an outage
    assert _rows([ok]) == {ok: 1}


# ------------------------------------------------------------- existing behaviour preserved
def test_healthy_database_writes_once_without_extra_queries(test_db):
    rid = f"h2-healthy-{uuid.uuid4().hex[:6]}"

    async def go():
        db = Database(_url(_settings().postgres_port), **FAST)
        assert await db.start() and not db.recovering
        recorder = RequestRecorder(db)
        await recorder.start()
        try:
            recorder.record(rec(rid))
            await recorder.flush()
            stored = await db.run(lambda s: s.scalars(select(RequestLog.request_id)
                                                      .where(RequestLog.request_id == rid)).all())
            return stored, recorder.retries, recorder.deduplicated
        finally:
            await recorder.stop()
            await db.close()

    stored, retries, deduplicated = asyncio.run(go())
    assert stored == [rid] and retries == 0 and deduplicated == 0
