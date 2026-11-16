"""Phase 11.2.2: benchmark setup never connects to `llm_router` or a maintenance database.

`probe_postgres` connects DIRECTLY to the existing `llm_router_test`, verifies it with
current_database(), never creates databases and never falls back; `scenario_postgres`
refuses every database but `llm_router_test` before connecting. No benchmark is executed
here: `main()` is exercised with run_benchmarks / write_results stubbed out. The conftest
connection guard additionally fails any test that attempts a forbidden connection.
"""

import asyncio
import inspect

import psycopg
import pytest

from benchmarks import run as bench_run
from benchmarks.scenarios import (
    BENCH_TEST_DB,
    FORBIDDEN_DBS,
    BenchConfig,
    BenchmarkDatabaseError,
    require_bench_database,
    scenario_postgres,
)
from tests.conftest import _pg_target_database


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
        self.targets.append(_pg_target_database(conninfo, kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return _FakeConn(self.outcome)


@pytest.fixture
def spy(monkeypatch):
    def install(outcome) -> ConnectSpy:
        s = ConnectSpy(outcome)
        monkeypatch.setattr(psycopg, "connect", s)
        return s

    return install


# --------------------------------------------------------------------------- #
# 1. probe_postgres: one direct attempt to llm_router_test, no fallback, clear errors
# --------------------------------------------------------------------------- #
def test_probe_connects_directly_to_test_database(spy):
    s = spy(BENCH_TEST_DB)
    assert bench_run.probe_postgres() is True
    assert bench_run.probe_postgres(BENCH_TEST_DB) is True
    assert s.targets == [BENCH_TEST_DB, BENCH_TEST_DB]


def test_probe_missing_database_is_a_clear_error_without_fallback(spy):
    s = spy(psycopg.OperationalError(f'connection failed: FATAL:  database "{BENCH_TEST_DB}" does not exist'))
    with pytest.raises(BenchmarkDatabaseError) as info:
        bench_run.probe_postgres()
    message = str(info.value)
    assert BENCH_TEST_DB in message and "does not exist" in message and "never create" in message
    assert s.targets == [BENCH_TEST_DB]  # no catalog lookup via llm_router / postgres, no CREATE


def test_probe_unreachable_server_is_a_clear_error_without_secrets(spy, monkeypatch):
    secret = "pw-Bench-Secret-987"
    monkeypatch.setenv("POSTGRES_PASSWORD", secret)
    s = spy(psycopg.OperationalError(f"connection refused (password={secret})"))
    with pytest.raises(BenchmarkDatabaseError) as info:
        bench_run.probe_postgres()
    assert "cannot connect" in str(info.value) and secret not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__  # driver error not chained
    assert s.targets == [BENCH_TEST_DB]


def test_probe_rejects_a_connection_that_lands_on_another_database(spy):
    spy("llm_router")
    with pytest.raises(BenchmarkDatabaseError, match="instead of"):
        bench_run.probe_postgres()


@pytest.mark.parametrize("dbname", sorted(FORBIDDEN_DBS) + ["some_other_db", ""])
def test_probe_refuses_every_other_database_before_connecting(spy, dbname):
    s = spy(BENCH_TEST_DB)
    with pytest.raises(BenchmarkDatabaseError, match="refusing"):
        bench_run.probe_postgres(dbname)
    assert s.targets == []


def test_probe_has_no_catalog_lookup_or_database_creation():
    source = inspect.getsource(bench_run.probe_postgres)
    for banned in ("pg_database", "CREATE DATABASE", "postgres_db"):
        assert banned not in source


# --------------------------------------------------------------------------- #
# 2. scenario_postgres refuses non-test databases before connecting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dbname", sorted(FORBIDDEN_DBS) + ["some_other_db"])
def test_scenario_postgres_refuses_non_test_databases(spy, dbname):
    s = spy(BENCH_TEST_DB)
    with pytest.raises(BenchmarkDatabaseError):
        asyncio.run(scenario_postgres(BenchConfig(postgres_db=dbname)))
    assert s.targets == []


def test_require_bench_database_allows_only_the_test_database():
    require_bench_database(BENCH_TEST_DB)
    assert issubclass(BenchmarkDatabaseError, RuntimeError)  # existing callers catch RuntimeError
    assert BENCH_TEST_DB not in FORBIDDEN_DBS


# --------------------------------------------------------------------------- #
# 3. main(): aborts clearly on an unusable test database; probes only when needed
#    (run_benchmarks / write_results are stubbed: no benchmark is executed)
# --------------------------------------------------------------------------- #
@pytest.fixture
def no_benchmark_execution(monkeypatch):
    calls: list[str] = []

    async def fake_run(cfg, groups):
        calls.append("run")
        return {"results": [], "skipped_groups": {}}

    def fake_write(report, output):
        calls.append("write")
        return []

    monkeypatch.setattr(bench_run, "run_benchmarks", fake_run)
    monkeypatch.setattr(bench_run, "write_results", fake_write)
    monkeypatch.setattr(bench_run, "format_table", lambda report: "")
    monkeypatch.setattr(bench_run, "probe_redis", lambda db: False)
    return calls


def test_main_aborts_with_clear_error_when_test_database_is_unusable(no_benchmark_execution, spy, capsys):
    s = spy(psycopg.OperationalError(f'FATAL:  database "{BENCH_TEST_DB}" does not exist'))
    assert bench_run.main(["--scenarios", "postgres", "--quick"]) == 2
    assert no_benchmark_execution == []  # aborted before any benchmark ran or results were written
    assert "benchmark aborted" in capsys.readouterr().err
    assert s.targets == [BENCH_TEST_DB]


def test_main_does_not_probe_postgres_when_no_group_needs_it(no_benchmark_execution, monkeypatch):
    def must_not_probe(*args, **kwargs):
        raise AssertionError("PostgreSQL probed although no requested group needs it")

    monkeypatch.setattr(bench_run, "probe_postgres", must_not_probe)
    assert bench_run.main(["--scenarios", "micro", "--quick"]) == 0
    assert no_benchmark_execution == ["run", "write"]  # stubs only


def test_main_probes_only_the_test_database(no_benchmark_execution, spy):
    s = spy(BENCH_TEST_DB)
    assert bench_run.main(["--scenarios", "postgres", "--quick"]) == 0
    assert s.targets == [BENCH_TEST_DB]


# --------------------------------------------------------------------------- #
# 4. Real server (skipped when PostgreSQL is down): one connection, to llm_router_test only
# --------------------------------------------------------------------------- #
def test_real_probe_targets_only_the_test_database(postgres_available, monkeypatch):
    if not postgres_available:
        pytest.skip("PostgreSQL not reachable on 127.0.0.1:5433")
    real = psycopg.connect
    targets: list[str] = []

    def recording(conninfo: str = "", **kwargs):
        targets.append(_pg_target_database(conninfo, kwargs))
        return real(conninfo, **kwargs)

    monkeypatch.setattr(psycopg, "connect", recording)
    assert bench_run.probe_postgres() is True
    assert targets == [BENCH_TEST_DB]
