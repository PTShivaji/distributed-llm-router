"""Phase 11.2.1: the test suite never connects to the production database `llm_router`.

Test setup discovers the isolated database by connecting to `llm_router_test` DIRECTLY;
it never queries another database's catalog, never creates databases and never falls back
to `llm_router` or the `postgres` maintenance database. A session-wide guard in conftest
refuses (before any network I/O) every psycopg / SQLAlchemy connection aimed at a
forbidden database and fails the test that attempted it.

Every guard test below targets 127.0.0.1:1 (nothing listens there), so even a broken
guard could never reach a real PostgreSQL server.
"""

import asyncio
import inspect
from contextlib import contextmanager

import psycopg
import pytest
import sqlalchemy

from tests import conftest as cf

NOWHERE = "host=127.0.0.1 port=1 connect_timeout=1"


@contextmanager
def expect_blocked(*names: str):
    """Asserts the guard blocked exactly these targets, then clears them (so the
    autouse `_no_forbidden_pg_connections` check does not fail this test)."""
    before = len(cf.PG_BLOCKED_CONNECTIONS)
    try:
        yield
    finally:
        got = cf.PG_BLOCKED_CONNECTIONS[before:]
        del cf.PG_BLOCKED_CONNECTIONS[before:]
    assert got == list(names)


class _FakeConn:
    def __init__(self, dbname: str) -> None:
        self.dbname = dbname

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return self

    def fetchone(self):
        return (self.dbname,)


class ConnectSpy:
    """Stands in for psycopg.connect: records the target database of every attempt."""

    def __init__(self, outcome) -> None:
        self.targets: list[str] = []
        self.outcome = outcome

    def __call__(self, conninfo: str = "", **kwargs):
        self.targets.append(cf._pg_target_database(conninfo, kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return _FakeConn(self.outcome)


# --------------------------------------------------------------------------- #
# 1. Test-database discovery connects to llm_router_test only, with no fallback
# --------------------------------------------------------------------------- #
def test_probe_connects_directly_to_test_database(monkeypatch):
    spy = ConnectSpy(cf.TEST_PG_DB)
    monkeypatch.setattr(psycopg, "connect", spy)
    assert cf._probe_test_database() == ("ok", "")
    assert spy.targets == [cf.TEST_PG_DB]


def test_missing_test_database_does_not_fall_back(monkeypatch):
    spy = ConnectSpy(psycopg.OperationalError(
        f'connection failed: FATAL:  database "{cf.TEST_PG_DB}" does not exist'))
    monkeypatch.setattr(psycopg, "connect", spy)
    status, detail = cf._probe_test_database()
    assert status == "missing" and cf.TEST_PG_DB in detail
    assert spy.targets == [cf.TEST_PG_DB]  # one attempt; no llm_router / postgres lookup


def test_unreachable_server_does_not_fall_back_or_leak_secrets(monkeypatch):
    secret = "pw-Secret-Value-123"
    monkeypatch.setenv("POSTGRES_PASSWORD", secret)
    spy = ConnectSpy(psycopg.OperationalError(f"connection refused (password={secret})"))
    monkeypatch.setattr(psycopg, "connect", spy)
    status, detail = cf._probe_test_database()
    assert status == "unreachable" and secret not in detail
    assert spy.targets == [cf.TEST_PG_DB]


def test_probe_rejects_a_connection_that_lands_on_another_database(monkeypatch):
    monkeypatch.setattr(psycopg, "connect", ConnectSpy("llm_router"))
    assert cf._probe_test_database()[0] != "ok"


def test_missing_test_database_is_a_clear_hard_error():
    with pytest.raises(pytest.fail.Exception) as info:
        cf._require_test_database("missing", "database 'llm_router_test' does not exist")
    message = str(info.value)
    assert cf.TEST_PG_DB in message and "CREATE DATABASE" in message and "never creates" in message
    assert cf._require_test_database("ok", "") is True
    assert cf._require_test_database("unreachable", "down") is False  # skip, as before


@pytest.mark.parametrize("dbname", ["llm_router", "postgres", "template1", "anything_else"])
def test_conninfo_helper_refuses_every_other_database(dbname):
    with pytest.raises(ValueError):
        cf._postgres_admin_conninfo(dbname)


def test_conninfo_helper_targets_test_database_by_default():
    assert cf._pg_target_database(cf._postgres_admin_conninfo(), {}) == cf.TEST_PG_DB


def test_setup_code_has_no_catalog_lookup_or_database_creation():
    assert not hasattr(cf, "_ensure_test_database")
    source = inspect.getsource(cf._probe_test_database) + inspect.getsource(cf._postgres_admin_conninfo)
    for banned in ("pg_database", "CREATE DATABASE", "postgres_db", '"postgres"', "'postgres'"):
        assert banned not in source


# --------------------------------------------------------------------------- #
# 2. The session-wide connection guard
# --------------------------------------------------------------------------- #
def test_guard_is_installed_on_every_psycopg_entry_point():
    assert getattr(psycopg.Connection.connect, "_llmr_guard", False)
    assert getattr(psycopg.AsyncConnection.connect, "_llmr_guard", False)
    assert getattr(psycopg.connect, "_llmr_guard", False)


@pytest.mark.parametrize("dbname", sorted(cf.FORBIDDEN_PG_DBS))
def test_guard_blocks_forbidden_conninfo_targets(dbname):
    with expect_blocked(dbname), pytest.raises(cf.ForbiddenDatabaseConnection):
        psycopg.connect(f"{NOWHERE} dbname={dbname}")


def test_guard_blocks_keyword_url_and_implicit_targets(monkeypatch):
    monkeypatch.delenv("PGDATABASE", raising=False)
    with expect_blocked("llm_router", "postgres", "llm_router"):
        with pytest.raises(cf.ForbiddenDatabaseConnection):
            psycopg.connect(host="127.0.0.1", port=1, dbname="llm_router")
        with pytest.raises(cf.ForbiddenDatabaseConnection):
            psycopg.Connection.connect("postgresql://router@127.0.0.1:1/postgres")
        with pytest.raises(cf.ForbiddenDatabaseConnection):  # libpq: no dbname -> user name
            psycopg.connect(f"{NOWHERE} user=llm_router")


def test_guard_blocks_pgdatabase_environment_default(monkeypatch):
    monkeypatch.setenv("PGDATABASE", "llm_router")
    with expect_blocked("llm_router"), pytest.raises(cf.ForbiddenDatabaseConnection):
        psycopg.connect(f"{NOWHERE} user=router")


def test_guard_blocks_async_connections():
    async def go():
        await psycopg.AsyncConnection.connect(f"{NOWHERE} dbname=llm_router")

    with expect_blocked("llm_router"), pytest.raises(cf.ForbiddenDatabaseConnection):
        asyncio.run(go())


def test_guard_blocks_sqlalchemy_engines():
    engine = sqlalchemy.create_engine("postgresql+psycopg://router:x@127.0.0.1:1/llm_router",
                                      connect_args={"connect_timeout": 1})
    try:
        with expect_blocked("llm_router"), pytest.raises(cf.ForbiddenDatabaseConnection):
            engine.connect()
    finally:
        engine.dispose()


def test_guard_lets_the_test_database_through():
    before = len(cf.PG_BLOCKED_CONNECTIONS)
    with pytest.raises(psycopg.OperationalError):  # refused by the network, not by the guard
        psycopg.connect(f"{NOWHERE} dbname={cf.TEST_PG_DB}")
    assert len(cf.PG_BLOCKED_CONNECTIONS) == before


def test_swallowed_forbidden_attempt_still_fails_the_test():
    before = len(cf.PG_BLOCKED_CONNECTIONS)
    try:
        psycopg.connect(f"{NOWHERE} dbname=llm_router")
    except cf.ForbiddenDatabaseConnection:
        pass  # application code that catches everything must not hide the attempt
    with pytest.raises(pytest.fail.Exception, match="llm_router"):
        cf._fail_on_blocked_since(before)
    assert len(cf.PG_BLOCKED_CONNECTIONS) == before  # cleared after reporting


# --------------------------------------------------------------------------- #
# 3. Real server (skipped when PostgreSQL is down): discovery uses llm_router_test only
# --------------------------------------------------------------------------- #
def test_real_probe_targets_only_the_test_database(postgres_available, monkeypatch):
    if not postgres_available:
        pytest.skip("PostgreSQL not reachable on 127.0.0.1:5433")
    real = psycopg.connect
    targets: list[str] = []

    def spy(conninfo: str = "", **kwargs):
        targets.append(cf._pg_target_database(conninfo, kwargs))
        return real(conninfo, **kwargs)

    monkeypatch.setattr(psycopg, "connect", spy)
    assert cf._probe_test_database() == ("ok", "")
    assert targets == [cf.TEST_PG_DB]
