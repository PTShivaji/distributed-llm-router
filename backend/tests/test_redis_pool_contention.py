"""Phase 10.1: Redis connection-pool contention is bounded and is never mistaken for an outage.

Before the fix the shared pool raised MaxConnectionsError the moment all
connections were busy. Every feature treated that as "Redis down", so under a
burst the rate limiter failed open and clients exceeded their limit (Phase 10:
75 simultaneous requests at 10/min -> up to 75 x 200, 0 x 429).
"""

import asyncio
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import BlockingConnectionPool

from app.core.config import Settings
from app.infra.container import build_container
from app.infra.redis import REDIS_ERRORS, BoundedWaitConnectionPool, RedisManager, RedisPoolExhaustedError
from app.main import create_app
from app.ratelimit.limiter import RateLimiter
from tests.conftest import TEST_HOST, TEST_REDIS_DB, redis_manager

QUERY = "What is the capital of France?"


# ------------------------------------------------------------------ 1. pool construction
def test_manager_builds_bounded_blocking_pool_from_settings(infra_settings):
    assert Settings.model_fields["redis_max_connections"].default == 50  # default pool size unchanged
    container = build_container(infra_settings(redis_max_connections=7))

    async def go():
        assert await container.redis.start() is True
        try:
            pool = container.redis._pool
            return type(pool), pool.max_connections, pool.timeout
        finally:
            await container.redis.close()

    pool_type, size, timeout = asyncio.run(go())
    assert pool_type is BoundedWaitConnectionPool and issubclass(pool_type, BlockingConnectionPool)
    assert size == 7  # REDIS_MAX_CONNECTIONS is honoured
    assert timeout == 1.0  # bounded wait, never indefinite
    assert not container.redis.started  # lifecycle: close() still tears the pool down


# ------------------------------------------------------------------ 2. wait, then succeed
def test_waiting_request_is_served_when_a_connection_is_released(redis_prefix):
    async def go():
        m = redis_manager(redis_prefix, max_connections=1, pool_timeout=5.0)
        await m.start()
        try:
            held = await m._pool.get_connection()
            waiter = asyncio.create_task(m.client.set(m.keys("waiter"), "1"))
            await asyncio.sleep(0.2)
            assert not waiter.done()  # blocked on the full pool - not failed
            await m._pool.release(held)
            assert await asyncio.wait_for(waiter, 5) is True
            return await m.client.get(m.keys("waiter"))
        finally:
            await m.close()

    assert asyncio.run(go()) == b"1"


# ------------------------------------------------------------------ 3. bounded timeout
def test_acquisition_times_out_within_bound_with_pool_exhausted_error(redis_prefix):
    async def go():
        m = redis_manager(redis_prefix, max_connections=1, pool_timeout=0.3)
        await m.start()
        try:
            await m._pool.get_connection()  # the only connection is now busy
            t0 = time.perf_counter()
            with pytest.raises(RedisPoolExhaustedError) as err:
                await m.client.ping()
            return err.value, time.perf_counter() - t0
        finally:
            await m.close()

    exc, waited = asyncio.run(go())
    assert isinstance(exc, REDIS_ERRORS)  # still caught by every feature that degrades
    assert 0.25 <= waited < 3.0  # waited for the bound, then gave up (no retries, no hang)


# ------------------------------------------------------------------ 4. release + recovery
def test_connections_are_released_and_pool_recovers_after_contention(redis_prefix):
    async def go():
        m = redis_manager(redis_prefix, max_connections=3, pool_timeout=5.0)
        await m.start()
        pool = m._pool
        try:
            async def op(i):
                key = m.keys("op", str(i % 20))
                await m.client.set(key, i)
                async with m.client.pipeline(transaction=False) as pipe:
                    pipe.get(key)
                    pipe.exists(key)
                    await pipe.execute()
                return await m.client.eval("return 1", 0)

            results = await asyncio.gather(*[op(i) for i in range(300)])  # 300 concurrent ops, 3 connections
            assert results == [1] * 300
            assert len(pool._in_use_connections) == 0 and len(pool._available_connections) <= 3

            held = [await pool.get_connection() for _ in range(3)]
            pool.timeout = 0.2
            for _ in range(3):
                with pytest.raises(RedisPoolExhaustedError):
                    await m.client.ping()
            assert len(pool._in_use_connections) == 3  # failed waits took nothing from the pool
            for conn in held:
                await pool.release(conn)
            assert await m.client.ping() is True
            assert len(pool._in_use_connections) == 0  # fully recovered, nothing leaked
        finally:
            await m.close()

    asyncio.run(go())


# ------------------------------------------------------------------ 5. exact burst limit
def test_rate_limit_is_exact_for_bursts_larger_than_the_pool(redis_prefix, infra_settings):
    async def limiter_burst():
        m = redis_manager(redis_prefix, max_connections=2)
        await m.start()
        try:
            rl = RateLimiter(m, per_minute=10, per_hour=0, fail_open=True)
            return await asyncio.gather(*[rl.check_client("burst-client") for _ in range(60)])
        finally:
            await m.close()

    burst = asyncio.run(limiter_burst())
    assert sum(r.allowed for r in burst) == 10
    assert not any(r.degraded or r.busy for r in burst)  # contention waited; nothing failed open

    # End to end: 5 clients x 15 simultaneous requests through a 5-connection pool, 10/min each.
    keys = {f"client{i}": f"phase10-1-burst-key-{i}-not-a-secret" for i in range(5)}
    s = infra_settings(chat_auth_mode="api_key", chat_api_keys=keys, rate_limit_enabled=True,
                       rate_limit_per_minute=10, rate_limit_per_hour=0, rate_limit_fail_open=True,
                       redis_max_connections=5, cache_enabled=False, health_backend="memory")
    jobs = [(name, n) for name in keys for n in range(15)]
    with TestClient(create_app(s)) as c:
        def send(job):
            name, n = job
            r = c.post("/api/v1/chat", json={"query": f"burst {name} {n}"},
                       headers={"Authorization": f"Bearer {keys[name]}"})
            return name, r

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            results = list(pool.map(send, jobs))
    for name in keys:
        codes = sorted(r.status_code for n, r in results if n == name)
        assert codes == [200] * 10 + [429] * 5, (name, codes)
    rejected = [r for _, r in results if r.status_code == 429]
    assert all(r.json()["error"]["code"] == "rate_limit_exceeded" for r in rejected)
    assert all(1 <= int(r.headers["Retry-After"]) <= 60 for r in rejected)


# ------------------------------------------------------------------ 6. fail-open / fail-closed
@pytest.mark.parametrize("fail_open", [True, False])
def test_fail_open_setting_applies_to_outages_but_never_to_pool_contention(fail_open, infra_settings):
    # (a) genuine outage, many concurrent checks through a tiny pool: RATE_LIMIT_FAIL_OPEN is honoured
    async def outage():
        m = RedisManager(f"redis://{TEST_HOST}:1/{TEST_REDIS_DB}", connect_timeout=0.3, socket_timeout=0.3,
                         max_connections=2, pool_timeout=0.2)
        await m.start()
        try:
            rl = RateLimiter(m, per_minute=10, per_hour=0, fail_open=fail_open)
            return await asyncio.gather(*[rl.check_client(f"c{i}") for i in range(12)])
        finally:
            await m.close()

    results = asyncio.run(outage())
    assert all(r.degraded and r.allowed is fail_open and not r.busy for r in results)

    # (b) the same outage end to end
    down = infra_settings(redis_port=1, redis_connect_timeout_seconds=0.3, redis_socket_timeout_seconds=0.3,
                          rate_limit_enabled=True, rate_limit_fail_open=fail_open)
    with TestClient(create_app(down)) as c:
        r = c.post("/api/v1/chat", json={"query": QUERY})
    if fail_open:
        assert r.status_code == 200
    else:
        assert r.status_code == 503 and r.json()["error"]["code"] == "rate_limiter_unavailable"

    # (c) pool contention while Redis is healthy: rejected (503 + Retry-After) whatever the setting
    busy = infra_settings(rate_limit_enabled=True, rate_limit_per_minute=100, rate_limit_fail_open=fail_open,
                          redis_max_connections=1)
    with TestClient(create_app(busy)) as c:
        pool = c.app.state.container.redis._pool
        held = c.portal.call(pool.get_connection)
        try:
            r = c.post("/api/v1/chat", json={"query": QUERY})
        finally:
            c.portal.call(pool.release, held)
        after = c.post("/api/v1/chat", json={"query": QUERY})
    assert r.status_code == 503 and r.headers["Retry-After"] == "1"
    assert r.json()["error"]["code"] == "rate_limiter_unavailable"
    assert after.status_code == 200


# ------------------------------------------------------------------ 7. cache + health recovery
class _CuttableProxy:
    """TCP proxy in front of the test Redis: cut() simulates an outage, restore() brings it back."""

    def __init__(self) -> None:
        self.up = True
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
            upstream = socket.create_connection((TEST_HOST, 6379))
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


def test_cache_and_health_recover_after_contention_and_after_redis_restoration(infra_settings):
    def chat(c, query):
        r = c.post("/api/v1/chat", json={"query": query})
        assert r.status_code == 200
        return r.json()

    # (a) contention: cache and health degrade while the pool is held, then recover on release
    s = infra_settings(rate_limit_enabled=False, cache_enabled=True, health_backend="redis", redis_max_connections=1)
    with TestClient(create_app(s)) as c:
        container = c.app.state.container
        pool = container.redis._pool
        pool.timeout = 0.2  # keep the test quick; the bound itself is covered above
        held = c.portal.call(pool.get_connection)
        try:
            body = chat(c, "contention question about hash maps")
            assert body["cache"]["status"] == "error" and container.health.degraded
        finally:
            c.portal.call(pool.release, held)
        miss = chat(c, "contention question about hash maps")
        assert miss["cache"]["status"] == "miss" and miss["cache"]["stored"]
        assert not container.health.degraded  # health tracking is back on Redis
        hit = chat(c, "contention question about hash maps")
        assert hit["cache"]["status"] == "hit_exact" and hit["answer"] == miss["answer"]

    # (b) genuine outage and restoration (through a proxy - the real Redis is never touched)
    proxy = _CuttableProxy()
    try:
        s = infra_settings(rate_limit_enabled=True, rate_limit_fail_open=True, cache_enabled=True,
                           health_backend="redis", redis_port=proxy.port)
        with TestClient(create_app(s)) as c:
            container = c.app.state.container
            first = chat(c, "restoration question about queues")
            assert first["cache"]["status"] == "miss" and first["cache"]["stored"]

            proxy.cut()
            during = chat(c, "restoration question about stacks")  # fail-open + degraded cache/health
            assert during["cache"]["status"] == "error" and container.health.degraded

            proxy.restore()
            again = chat(c, "restoration question about queues")
            assert again["cache"]["status"] == "hit_exact" and again["answer"] == first["answer"]
            assert not container.health.degraded
    finally:
        proxy.close()
