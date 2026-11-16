"""PostgreSQL access via SQLAlchemy 2.x + psycopg 3.

Design note: SQLAlchemy's asyncio extension requires `greenlet`, which is not
installed in this project. To avoid adding a dependency, the (synchronous)
engine is used from worker threads via `asyncio.to_thread`, so database I/O
never blocks FastAPI's event loop. The connection pool is thread-safe.

Recovery (Phase 11.2, H2). `available` means "connected and migrated". It used
to be decided ONCE at startup: if PostgreSQL was down then, persistence,
analytics and budget history stayed off until the process restarted. Now an
unavailable database (at startup, or reported by the request recorder after
repeated write failures) starts ONE background reconnect loop with bounded
exponential backoff (DATABASE_RECONNECT_INITIAL_SECONDS doubling up to
DATABASE_RECONNECT_MAX_SECONDS, +-20% jitter). Each attempt pings and runs the
(idempotent, advisory-locked) migrations; on success `available` flips back and
everything that checks it resumes without a restart.
"""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import log_event
from app.db.migrations import run_migrations

logger = logging.getLogger("app.infra.database")
T = TypeVar("T")

DB_ERRORS: tuple[type[BaseException], ...] = (SQLAlchemyError, OSError)


class DatabaseUnavailableError(RuntimeError):
    pass


# Connection-level failures: the same operation can succeed once PostgreSQL is reachable again.
# Everything else in DB_ERRORS (IntegrityError, DataError, ProgrammingError, ...) is permanent.
TRANSIENT_DB_ERRORS: tuple[type[BaseException], ...] = (
    OperationalError, InterfaceError, DisconnectionError, PoolTimeoutError, OSError, DatabaseUnavailableError,
)


class Database:
    def __init__(
        self,
        url: str,
        *,
        enabled: bool = True,
        auto_migrate: bool = True,
        pool_size: int = 5,
        connect_timeout: int = 3,
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self._url = url  # never logged: contains the password
        self.enabled = enabled
        self.auto_migrate = auto_migrate
        self._pool_size = pool_size
        self._connect_timeout = connect_timeout
        self.reconnect_initial_seconds = reconnect_initial_seconds
        self.reconnect_max_seconds = max(reconnect_max_seconds, reconnect_initial_seconds)
        self.engine: Engine | None = None
        self._sessions: sessionmaker[Session] | None = None
        self.metrics: Any = None  # set by the container (db availability / reconnect metrics)
        self._available = False
        self._available_event: asyncio.Event | None = None
        self._recovery_task: asyncio.Task[None] | None = None
        self._closing = False
        self._unavailable_since: float | None = None
        self.reconnect_attempts = 0  # total attempts by the recovery loop (diagnostics/tests)
        self.recoveries = 0

    # ------------------------------------------------------------------ availability
    @property
    def available(self) -> bool:
        return self._available

    @available.setter
    def available(self, value: bool) -> None:
        self._available = value
        if self._available_event is not None:
            (self._available_event.set if value else self._available_event.clear)()
        if self.metrics is not None:
            self.metrics.db_set_available(value)

    async def wait_available(self) -> None:
        """Block until the database is available (used by the background recorder only)."""
        if self._available:
            return
        if self._available_event is None:
            self._available_event = asyncio.Event()
        await self._available_event.wait()

    @property
    def recovering(self) -> bool:
        return self._recovery_task is not None and not self._recovery_task.done()

    def mark_unavailable(self, reason: str) -> None:
        """Report a connection-level failure; start the reconnect loop if it is not running."""
        if not self.enabled or self._closing or self.engine is None:
            return
        if self._available:
            self.available = False
            log_event(logger, "db.unavailable", logging.ERROR, reason=reason)
        self._start_recovery(reason)

    def _start_recovery(self, reason: str) -> None:
        if self.recovering or self._closing:
            return
        self._unavailable_since = self._unavailable_since or time.monotonic()
        self._recovery_task = asyncio.create_task(self._recover(reason), name="db-reconnect")

    async def _recover(self, reason: str) -> None:
        delay = self.reconnect_initial_seconds
        attempt = 0
        while not self._closing:
            await asyncio.sleep(min(delay * random.uniform(0.8, 1.2), self.reconnect_max_seconds))
            attempt += 1
            self.reconnect_attempts += 1
            try:
                await asyncio.to_thread(self._connect_sync)
            except DB_ERRORS as exc:
                if self.metrics is not None:
                    self.metrics.db_reconnect_attempt("failure")
                delay = min(delay * 2, self.reconnect_max_seconds)
                log_event(logger, "db.reconnect_failed", logging.WARNING, attempt=attempt, error=type(exc).__name__,
                          next_retry_s=round(delay, 2), reason=reason)
                continue
            if self.metrics is not None:
                self.metrics.db_reconnect_attempt("success")
            downtime = time.monotonic() - (self._unavailable_since or time.monotonic())
            self._unavailable_since = None
            self.recoveries += 1
            self.available = True
            log_event(logger, "db.recovered", attempts=attempt, downtime_s=round(downtime, 2), reason=reason)
            return

    # ------------------------------------------------------------------ lifecycle
    def _create_engine(self) -> None:
        self.engine = create_engine(
            self._url,
            pool_size=self._pool_size,
            max_overflow=self._pool_size,
            pool_pre_ping=True,
            connect_args={"connect_timeout": self._connect_timeout},
        )
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def _connect_sync(self) -> None:
        """Ping, then apply pending migrations (idempotent, serialised by an advisory lock)."""
        self._ping_sync()
        if self.auto_migrate:
            applied = run_migrations(self.engine)
            log_event(logger, "db.migrations_checked", applied=len(applied))

    async def start(self) -> bool:
        """Create the engine, check connectivity, run migrations. Never raises.

        If PostgreSQL is unreachable the app still starts; a background reconnect loop
        makes the database available as soon as it can (no restart needed)."""
        if not self.enabled:
            log_event(logger, "db.disabled")
            return False
        self._closing = False
        self._available_event = asyncio.Event()
        if self.engine is None:
            self._create_engine()
        try:
            await asyncio.to_thread(self._connect_sync)
            self.available = True
        except DB_ERRORS as exc:
            self.available = False
            log_event(logger, "db.unavailable", logging.WARNING, error=type(exc).__name__, reason="startup",
                      reconnect=True)
            self._start_recovery("startup")
        log_event(logger, "db.started", available=self.available)
        return self.available

    async def close(self) -> None:
        self._closing = True
        task, self._recovery_task = self._recovery_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.engine is not None:
            await asyncio.to_thread(self.engine.dispose)
        self.engine, self._sessions = None, None
        self.available = False
        log_event(logger, "db.closed")

    def _ping_sync(self) -> None:
        assert self.engine is not None
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    async def ping(self) -> bool:
        if not self.enabled or self.engine is None:
            return False
        try:
            await asyncio.to_thread(self._ping_sync)
            return True
        except DB_ERRORS:
            return False

    async def run(self, fn: Callable[[Session], T]) -> T:
        """Run `fn(session)` in a worker thread inside a transaction."""
        if not self.enabled or self._sessions is None:
            raise DatabaseUnavailableError("database is disabled or not started")
        sessions = self._sessions

        def work() -> T:
            with sessions.begin() as session:
                return fn(session)

        return await asyncio.to_thread(work)
