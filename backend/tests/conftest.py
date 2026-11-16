"""Shared pytest fixtures."""

import os

# MUST run before any `app` import: app.core.config decides at import time
# whether to load the project-root .env. Tests never read the developer's .env
# (app.main builds a module-level app from get_settings() on import).
os.environ["LLMR_DISABLE_ENV_FILE"] = "1"
# `import litellm` would otherwise call load_dotenv() (loading the project-root .env
# into os.environ) and fetch a cost map from the network. See app/providers/litellm_env.py.
os.environ["LITELLM_MODE"] = "PRODUCTION"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
# Belt and braces: python-dotenv's load_dotenv() becomes a no-op in the test process.
# (pydantic-settings uses dotenv_values(), which ignores this flag; explicit env files still work.)
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from collections.abc import Callable, Iterator  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.classifier import QueryClassifier  # noqa: E402
from app.core.config import Settings, get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.providers.mock import MockProvider  # noqa: E402
from app.providers.pool import ProviderPool  # noqa: E402
from app.routing.health import HealthTracker  # noqa: E402
from app.routing.policy import RoutingPolicy  # noqa: E402
from app.routing.registry import ModelConfig, ModelRegistry  # noqa: E402
from app.routing.service import ChatService, ExecutionSettings  # noqa: E402


PHASE4_OFF = {
    "REDIS_ENABLED": "false",
    "DATABASE_ENABLED": "false",
    "CACHE_ENABLED": "false",
    "RATE_LIMIT_ENABLED": "false",
    "HEALTH_BACKEND": "memory",
}


@pytest.fixture(autouse=True)
def _hermetic_by_default(monkeypatch):
    """Phase 1-3 tests stay hermetic: no Redis / PostgreSQL unless a test opts in
    explicitly (Phase 4 tests pass their own isolated settings)."""
    for key, value in PHASE4_OFF.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def test_settings() -> Settings:
    # _env_file=None -> ignore any local .env so tests are deterministic.
    # Mock mode and failure settings are forced so real env vars (e.g. a real
    # OPENAI_API_KEY in the shell) can never cause a real provider call.
    return Settings(
        _env_file=None,
        environment="test",
        app_name="Test Router",
        app_version="9.9.9",
        llm_mock_mode=True,
        mock_failures={},
        mock_latency_ms=0,
        model_registry_file=None,
        provider_retry_backoff_seconds=0,
    )


@pytest.fixture
def app(test_settings: Settings):
    application = create_app(test_settings)
    # Route-level Depends(get_settings) must also see the test settings.
    application.dependency_overrides[get_settings] = lambda: test_settings
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    # raise_server_exceptions=False lets us assert on the 500 JSON envelope
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="session")
def classifier() -> QueryClassifier:
    return QueryClassifier()


# --------------------------------------------------------------------------- #
# Routing helpers
# --------------------------------------------------------------------------- #
def model(model_id: str, /, tier: str = "SIMPLE", **overrides: Any) -> ModelConfig:
    """Compact ModelConfig builder with placeholder pricing."""
    data: dict[str, Any] = {
        "id": model_id,
        "provider": "mock",
        "provider_model": f"mock/{model_id}",
        "tier": tier,
        "input_cost_per_million": 1.0,
        "output_cost_per_million": 2.0,
        "max_context_tokens": 8192,
        "max_output_tokens": 1024,
        "timeout_seconds": 5,
    }
    data.update(overrides)
    return ModelConfig.model_validate(data)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def make_service() -> Callable[..., ChatService]:
    def factory(
        models: list[ModelConfig],
        *,
        failures: dict[str, str] | None = None,
        report_usage: bool = False,
        provider: Any = None,
        health: HealthTracker | None = None,
        policy_kwargs: dict[str, Any] | None = None,
        **execution: Any,
    ) -> ChatService:
        registry = ModelRegistry(models)
        health = health or HealthTracker(failure_threshold=3, cooldown_seconds=30)
        mock = provider or MockProvider(failures or {}, report_usage=report_usage)
        return ChatService(
            classifier=QueryClassifier(),
            policy=RoutingPolicy(registry, health, **(policy_kwargs or {})),
            providers=ProviderPool(mock=mock, mock_mode=True),
            health=health,
            execution=ExecutionSettings(**{"retry_backoff_seconds": 0, **execution}),
            sleep=SleepRecorder(),
        )

    return factory


# --------------------------------------------------------------------------- #
# Phase 4: real Redis / PostgreSQL in ISOLATED namespaces (skipped if unreachable)
#   Redis:      logical DB 15 + unique key prefix per test; only that prefix is deleted.
#   PostgreSQL: the pre-existing database "llm_router_test" ONLY; only its tables are
#               truncated. The suite never connects to the development/production
#               database or to the server's maintenance databases (not even read-only),
#               and never creates databases: a missing llm_router_test is a hard error.
# --------------------------------------------------------------------------- #
TEST_REDIS_DB = 15
TEST_PG_DB = "llm_router_test"
# IPv4 literal on purpose: docker-compose.yml publishes ports on 127.0.0.1 only, and on
# Windows "localhost" resolves to ::1 first, which then times out instead of failing fast.
TEST_HOST = "127.0.0.1"
# Databases no test, fixture or helper may ever open (enforced by the guard below).
FORBIDDEN_PG_DBS = frozenset({"llm_router", "postgres", "template0", "template1"})


# --------------------------------------------------------------------------- #
# PostgreSQL connection guard (whole test session)
#   Every psycopg connection attempt in the test process (direct psycopg, SQLAlchemy
#   engines, async connections, pools) resolves its target database first; a forbidden
#   target is refused BEFORE any network I/O and recorded, and the test that caused it
#   fails (see _no_forbidden_pg_connections). Tests that must fake a connection should
#   monkeypatch psycopg.connect / Connection.connect on top of this guard.
# --------------------------------------------------------------------------- #
class ForbiddenDatabaseConnection(Exception):
    """Raised instead of connecting to a database the test suite must never touch."""


PG_BLOCKED_CONNECTIONS: list[str] = []  # target database names only; never credentials


def _pg_target_database(conninfo: str | None, kwargs: dict[str, Any]) -> str:
    """The database libpq would connect to: explicit dbname, else PGDATABASE, else the user name."""
    import getpass

    from psycopg.conninfo import conninfo_to_dict

    params = conninfo_to_dict(conninfo or "")
    return str(kwargs.get("dbname") or params.get("dbname") or os.environ.get("PGDATABASE")
               or kwargs.get("user") or params.get("user") or os.environ.get("PGUSER") or getpass.getuser())


# Plus whatever the environment names as the application database (default: llm_router).
_GUARDED_PG_DBS = (FORBIDDEN_PG_DBS | {Settings(_env_file=None).postgres_db}) - {TEST_PG_DB}


def _check_pg_target(conninfo: str | None, kwargs: dict[str, Any]) -> None:
    target = _pg_target_database(conninfo, kwargs)
    if target in _GUARDED_PG_DBS:
        PG_BLOCKED_CONNECTIONS.append(target)
        raise ForbiddenDatabaseConnection(
            f"blocked a PostgreSQL connection to {target!r}: tests may only connect to {TEST_PG_DB!r}")


def _install_pg_connection_guard() -> None:
    import psycopg

    if getattr(psycopg.Connection.connect, "_llmr_guard", False):
        return
    real_sync = psycopg.Connection.connect.__func__
    real_async = psycopg.AsyncConnection.connect.__func__

    def guarded_sync(cls, conninfo: str = "", **kwargs: Any):
        _check_pg_target(conninfo, kwargs)
        return real_sync(cls, conninfo, **kwargs)

    async def guarded_async(cls, conninfo: str = "", **kwargs: Any):
        _check_pg_target(conninfo, kwargs)
        return await real_async(cls, conninfo, **kwargs)

    guarded_sync._llmr_guard = guarded_async._llmr_guard = True  # type: ignore[attr-defined]
    psycopg.Connection.connect = classmethod(guarded_sync)  # type: ignore[method-assign,assignment]
    psycopg.AsyncConnection.connect = classmethod(guarded_async)  # type: ignore[method-assign,assignment]
    psycopg.connect = psycopg.Connection.connect  # the module alias SQLAlchemy and helpers call


_install_pg_connection_guard()


def _fail_on_blocked_since(before: int) -> None:
    new = PG_BLOCKED_CONNECTIONS[before:]
    if new:
        del PG_BLOCKED_CONNECTIONS[before:]
        pytest.fail(f"test attempted forbidden PostgreSQL connection(s) to {sorted(set(new))}; "
                    f"tests may only connect to {TEST_PG_DB!r}", pytrace=False)


@pytest.fixture(autouse=True)
def _no_forbidden_pg_connections() -> Iterator[None]:
    """Fails any test whose code tried to open a forbidden database (even if it swallowed the error)."""
    before = len(PG_BLOCKED_CONNECTIONS)
    yield
    _fail_on_blocked_since(before)


# --------------------------------------------------------------------------- #
# Redis connection guard (whole test session): logical DB 15 ONLY
#   Every redis-py connection (sync and asyncio, TCP/TLS/unix socket) is checked when it
#   connects, BEFORE any network I/O: a connection configured for any database other than
#   TEST_REDIS_DB is refused and recorded. Switching databases on an open connection
#   (SELECT / SWAPDB / MOVE to another DB) is refused the same way. Building a client or a
#   URL string is not I/O and is not affected. The test that triggered it fails (see
#   _no_forbidden_redis_access), even if application code swallowed the error.
# --------------------------------------------------------------------------- #
def _redis_connection_error() -> type[Exception]:
    import redis.exceptions

    return redis.exceptions.ConnectionError


class ForbiddenRedisDatabase(_redis_connection_error()):  # type: ignore[misc]
    """Raised instead of touching a Redis database other than DB 15 from the test suite.

    A redis ConnectionError on purpose: application code then degrades exactly as for an
    unreachable server (no changed control flow), while the autouse check fails the test."""


REDIS_BLOCKED_ACCESS: list[str] = []  # descriptions only (database numbers / command names)


def _refuse_redis(description: str) -> None:
    REDIS_BLOCKED_ACCESS.append(description)
    raise ForbiddenRedisDatabase(f"blocked Redis access ({description}): tests may only use DB {TEST_REDIS_DB}")


def _check_redis_db(db: Any) -> None:
    try:
        number = int(db or 0)
    except (TypeError, ValueError):
        number = -1
    if number != TEST_REDIS_DB:
        _refuse_redis(f"connect to db {db!r}")


def _check_redis_command(args: tuple[Any, ...]) -> None:
    if not args:
        return
    name = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
    name = name.upper()
    if name == "SWAPDB":
        _refuse_redis("SWAPDB")
    if name == "SELECT" and len(args) > 1:
        _check_redis_db(args[1])
    if name == "MOVE" and len(args) > 2:
        _check_redis_db(args[2])


def _install_redis_guard() -> None:
    import redis.asyncio.client as async_client
    import redis.asyncio.connection as async_connection
    import redis.client as sync_client
    import redis.connection as sync_connection

    if getattr(sync_connection.AbstractConnection.connect_check_health, "_llmr_guard", False):
        return
    real_sync_connect = sync_connection.AbstractConnection.connect_check_health
    real_async_connect = async_connection.AbstractConnection.connect_check_health
    real_sync_exec = sync_client.Redis.execute_command
    real_async_exec = async_client.Redis.execute_command

    def sync_connect(self, *args: Any, **kwargs: Any):
        _check_redis_db(getattr(self, "db", 0))
        return real_sync_connect(self, *args, **kwargs)

    async def async_connect(self, *args: Any, **kwargs: Any):
        _check_redis_db(getattr(self, "db", 0))
        return await real_async_connect(self, *args, **kwargs)

    def sync_exec(self, *args: Any, **options: Any):
        _check_redis_command(args)
        return real_sync_exec(self, *args, **options)

    async def async_exec(self, *args: Any, **options: Any):
        _check_redis_command(args)
        return await real_async_exec(self, *args, **options)

    for fn in (sync_connect, async_connect, sync_exec, async_exec):
        fn._llmr_guard = True  # type: ignore[attr-defined]
    sync_connection.AbstractConnection.connect_check_health = sync_connect  # type: ignore[method-assign]
    async_connection.AbstractConnection.connect_check_health = async_connect  # type: ignore[method-assign]
    sync_client.Redis.execute_command = sync_exec  # type: ignore[method-assign]
    async_client.Redis.execute_command = async_exec  # type: ignore[method-assign]


_install_redis_guard()


def _fail_on_redis_blocked_since(before: int) -> None:
    new = REDIS_BLOCKED_ACCESS[before:]
    if new:
        del REDIS_BLOCKED_ACCESS[before:]
        pytest.fail(f"test attempted forbidden Redis access: {sorted(set(new))}; "
                    f"tests may only use DB {TEST_REDIS_DB}", pytrace=False)


@pytest.fixture(autouse=True)
def _no_forbidden_redis_access() -> Iterator[None]:
    """Fails any test whose code tried to use a Redis database other than DB 15."""
    before = len(REDIS_BLOCKED_ACCESS)
    yield
    _fail_on_redis_blocked_since(before)


def _redis_reachable() -> bool:
    import redis

    try:
        return bool(redis.Redis(host=TEST_HOST, port=6379, db=TEST_REDIS_DB, socket_connect_timeout=0.5).ping())
    except Exception:
        return False


def _postgres_admin_conninfo(dbname: str = TEST_PG_DB) -> str:
    """libpq conninfo for the isolated test database. Any other database is refused."""
    if dbname != TEST_PG_DB:
        raise ValueError(f"test helpers connect to {TEST_PG_DB!r} only, not {dbname!r}")
    s = Settings(_env_file=None)
    return (f"host={TEST_HOST} port={s.postgres_port} user={s.postgres_user} "
            f"password={s.postgres_password.get_secret_value()} dbname={TEST_PG_DB} connect_timeout=2")


def _probe_test_database() -> tuple[str, str]:
    """Connect to llm_router_test DIRECTLY (no catalog lookup through another database).

    Returns ("ok" | "missing" | "unreachable", detail). Exactly one connection attempt,
    always to TEST_PG_DB; there is no fallback to any other database. The detail never
    contains the connection string or password.
    """
    import psycopg

    try:
        with psycopg.connect(_postgres_admin_conninfo(TEST_PG_DB), autocommit=True) as conn:
            name = conn.execute("SELECT current_database()").fetchone()[0]
    except psycopg.OperationalError as exc:
        if getattr(exc, "sqlstate", None) == "3D000" or "does not exist" in str(exc):
            return "missing", f"database {TEST_PG_DB!r} does not exist"
        return "unreachable", f"PostgreSQL not reachable on {TEST_HOST} ({type(exc).__name__})"
    if name != TEST_PG_DB:  # defensive: never run against anything else
        return "unreachable", f"connected database is {name!r}, expected {TEST_PG_DB!r}"
    return "ok", ""


MISSING_TEST_DB_HELP = (
    f"The isolated test database {TEST_PG_DB!r} does not exist. The test suite never creates "
    f"databases and never connects to the development or maintenance databases to do so. "
    f"Create it once yourself (e.g. `CREATE DATABASE {TEST_PG_DB};` as the PostgreSQL owner), "
    f"or run without PostgreSQL to skip the database tests."
)


def _require_test_database(status: str, detail: str) -> bool:
    """True when usable; False (tests skip) when the server is down; hard error when missing."""
    if status == "missing":
        pytest.fail(MISSING_TEST_DB_HELP, pytrace=False)
    return status == "ok"


@pytest.fixture(scope="session")
def redis_available() -> bool:
    return _redis_reachable()


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    return _require_test_database(*_probe_test_database())


@pytest.fixture
def redis_prefix(redis_available) -> Iterator[str]:
    if not redis_available:
        pytest.skip("Redis not reachable on 127.0.0.1:6379 (start it with: docker compose up -d)")
    import uuid

    import redis

    prefix = f"test:{uuid.uuid4().hex[:12]}:"
    yield prefix
    client = redis.Redis(host=TEST_HOST, port=6379, db=TEST_REDIS_DB)
    keys = list(client.scan_iter(match=prefix + "*", count=500))
    if keys:
        client.delete(*keys)


@pytest.fixture
def pg_clean(postgres_available) -> Iterator[None]:
    if not postgres_available:
        pytest.skip("PostgreSQL not reachable on 127.0.0.1:5433 (start it with: docker compose up -d)")
    import psycopg

    def truncate() -> None:
        with psycopg.connect(_postgres_admin_conninfo(TEST_PG_DB), autocommit=True) as conn:
            exists = conn.execute("SELECT to_regclass('public.request_log')").fetchone()[0]
            if exists:
                conn.execute("TRUNCATE request_log, provider_attempt RESTART IDENTITY CASCADE")

    truncate()
    yield
    truncate()


@pytest.fixture
def infra_settings(redis_prefix, monkeypatch) -> Callable[..., Settings]:
    """Settings with Redis enabled in an isolated namespace (DB off unless asked)."""
    for key in PHASE4_OFF:
        monkeypatch.delenv(key, raising=False)

    def factory(**overrides: Any) -> Settings:
        base: dict[str, Any] = dict(
            _env_file=None, environment="test", llm_mock_mode=True, mock_failures={}, model_registry_file=None,
            provider_retry_backoff_seconds=0, redis_db=TEST_REDIS_DB, redis_key_prefix=redis_prefix,
            redis_host=TEST_HOST, postgres_host=TEST_HOST, postgres_db=TEST_PG_DB, database_enabled=False, log_level="WARNING",
        )
        base.update(overrides)
        return Settings(**base)

    return factory


def redis_manager(prefix: str, **kw: Any):
    from app.infra.redis import RedisManager

    return RedisManager(f"redis://{TEST_HOST}:6379/{TEST_REDIS_DB}", key_prefix=prefix, **kw)


@pytest.fixture
def three_tier_models() -> list[ModelConfig]:
    return [
        model("fast", "SIMPLE", fallbacks=("balanced",)),
        model("balanced", "MEDIUM", fallbacks=("advanced",)),
        model("advanced", "COMPLEX", capabilities=("chat", "reasoning", "coding"), fallbacks=("balanced",)),
    ]
