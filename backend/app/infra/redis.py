"""Centralised async Redis connection management.

One RedisManager per process owns ONE connection pool that every distributed
feature (cache, health, rate limiting) shares. Connections are opened in the
FastAPI lifespan and closed on shutdown.

Redis being unavailable is never fatal: `start()` logs and continues, and
callers catch `REDIS_ERRORS` and degrade (each feature documents how).

Pool contention is NOT an outage: when all `max_connections` are busy, a
request waits up to `pool_timeout` seconds for one to be released (redis-py's
default pool fails immediately with MaxConnectionsError, which every feature
used to treat as "Redis down" - letting bursts bypass the rate limiter). If the
wait times out while Redis is healthy, `RedisPoolExhaustedError` is raised so
callers can tell contention apart from an outage.
"""

import logging
import time

from redis.asyncio import BlockingConnectionPool, Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from app.core.logging import log_event

logger = logging.getLogger("app.infra.redis")


class RedisUnavailableError(RedisError):
    """Raised when Redis is disabled or the manager has not been started."""


# Everything a caller must catch to degrade gracefully on a Redis outage.
REDIS_ERRORS: tuple[type[BaseException], ...] = (RedisError, OSError, TimeoutError, ConnectionError)

# redis-py's BlockingConnectionPool raises ConnectionError(_POOL_WAIT_TIMEOUT) from asyncio.TimeoutError.
_POOL_WAIT_TIMEOUT = "No connection available."


class RedisPoolExhaustedError(RedisConnectionError):
    """No pooled connection was released within `pool_timeout` while Redis itself was healthy.

    Subclasses ConnectionError, so it is still in REDIS_ERRORS: features that
    simply degrade (cache, health) keep doing so. The rate limiter treats it as
    contention, never as an outage (see RateLimiter.check).
    """


class BoundedWaitConnectionPool(BlockingConnectionPool):
    """Waits (bounded) for a free connection, then tells contention apart from an outage.

    A timed-out wait is part of a genuine outage - the plain ConnectionError is
    re-raised and callers degrade exactly as before - while a connect is still
    in flight or one failed within OUTAGE_MEMORY_SECONDS (requests queued behind
    connects to an unreachable Redis). Otherwise Redis is healthy and every
    connection was simply busy: RedisPoolExhaustedError.
    """

    OUTAGE_MEMORY_SECONDS = 5.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._connecting = 0
        self._last_connect_failure = float("-inf")

    async def ensure_connection(self, connection) -> None:
        connecting = not connection.is_connected
        self._connecting += connecting
        try:
            await super().ensure_connection(connection)
        except REDIS_ERRORS:
            self._last_connect_failure = time.monotonic()  # connect/handshake failed: Redis unreachable
            raise
        finally:
            self._connecting -= connecting

    async def get_connection(self, *args, **kwargs):
        try:
            return await super().get_connection(*args, **kwargs)
        except RedisConnectionError as exc:
            wait_timed_out = str(exc) == _POOL_WAIT_TIMEOUT and isinstance(exc.__cause__, TimeoutError)
            outage = self._connecting > 0 or time.monotonic() - self._last_connect_failure < self.OUTAGE_MEMORY_SECONDS
            if not wait_timed_out or outage:
                raise
            raise RedisPoolExhaustedError(
                f"No Redis connection was released within {self.timeout}s (pool of {self.max_connections})"
            ) from exc


class RedisKeys:
    """All keys are namespaced by a configurable prefix (multi-tenant / test isolation)."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def __call__(self, *parts: str) -> str:
        return self.prefix + ":".join(parts)

    def pattern(self, *parts: str) -> str:
        return self(*parts) + "*"


class RedisManager:
    def __init__(
        self,
        url: str,
        *,
        enabled: bool = True,
        key_prefix: str = "llmr:",
        connect_timeout: float = 1.0,
        socket_timeout: float = 1.0,
        max_connections: int = 50,
        pool_timeout: float = 1.0,
        health_check_interval: int = 30,
    ) -> None:
        self._url = url  # never logged: may contain a password
        self.enabled = enabled
        self.keys = RedisKeys(key_prefix)
        self._options = {
            "socket_connect_timeout": connect_timeout,
            "socket_timeout": socket_timeout,
            "max_connections": max_connections,
            "timeout": pool_timeout,  # bounded wait for a free pooled connection
            "health_check_interval": health_check_interval,
        }
        self._pool: BoundedWaitConnectionPool | None = None
        self._client: Redis | None = None

    # ------------------------------------------------------------------ #
    async def start(self) -> bool:
        """Create the shared pool and ping. Returns reachability; never raises."""
        if not self.enabled:
            log_event(logger, "redis.disabled")
            return False
        if self._client is None:
            self._pool = BoundedWaitConnectionPool.from_url(self._url, decode_responses=False, **self._options)
            self._client = Redis(connection_pool=self._pool)
        ok = await self.ping()
        log_event(logger, "redis.started", logging.INFO if ok else logging.WARNING, reachable=ok, prefix=self.keys.prefix)
        return ok

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._pool is not None:
            await self._pool.aclose()
        self._client = self._pool = None
        log_event(logger, "redis.closed")

    @property
    def started(self) -> bool:
        return self._client is not None

    @property
    def client(self) -> Redis:
        if not self.enabled or self._client is None:
            raise RedisUnavailableError("Redis is disabled or not started")
        return self._client

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except REDIS_ERRORS as exc:
            log_event(logger, "redis.ping_failed", logging.WARNING, error=type(exc).__name__)
            return False
