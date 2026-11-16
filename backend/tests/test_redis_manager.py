"""Redis connection lifecycle, health checks, key prefixes, outage behaviour."""

import asyncio
import time

import pytest

from app.infra.redis import RedisKeys, RedisManager, RedisUnavailableError
from tests.conftest import redis_manager


def test_key_prefixing():
    k = RedisKeys("app1:")
    assert k("cache", "entry", "x") == "app1:cache:entry:x"
    assert k.pattern("cache") == "app1:cache*"


def test_disabled_manager_never_connects():
    async def go():
        m = RedisManager("redis://localhost:6379/15", enabled=False)
        assert await m.start() is False
        with pytest.raises(RedisUnavailableError):
            _ = m.client
        assert await m.ping() is False

    asyncio.run(go())


def test_not_started_client_raises():
    with pytest.raises(RedisUnavailableError):
        _ = RedisManager("redis://localhost:6379/15").client


def test_unreachable_redis_start_does_not_raise_and_is_fast():
    async def go():
        m = RedisManager("redis://localhost:1/15", connect_timeout=0.3, socket_timeout=0.3)  # DB15: guard
        t0 = time.perf_counter()
        assert await m.start() is False
        assert time.perf_counter() - t0 < 3
        await m.close()

    asyncio.run(go())


def test_lifecycle_start_ping_close(redis_prefix):
    async def go():
        m = redis_manager(redis_prefix)
        assert await m.start() is True and m.started
        assert await m.ping() is True
        pool = m._pool
        await m.start()  # idempotent: does not create a second pool
        assert m._pool is pool
        await m.close()
        assert not m.started
        with pytest.raises(RedisUnavailableError):
            _ = m.client

    asyncio.run(go())


def test_container_shares_one_pool(infra_settings):
    from app.infra.container import build_container

    c = build_container(infra_settings())
    assert c.cache.redis is c.redis
    assert c.rate_limiter.redis is c.redis
    assert c.health.redis is c.redis


def test_app_starts_when_redis_down():
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    s = Settings(_env_file=None, redis_enabled=True, redis_port=1, redis_db=15, redis_connect_timeout_seconds=0.3,
                 redis_socket_timeout_seconds=0.3, database_enabled=False, cache_enabled=True,
                 rate_limit_enabled=True, health_backend="redis", llm_mock_mode=True, mock_failures={})
    with TestClient(create_app(s)) as c:
        r = c.post("/api/v1/chat", json={"query": "What is the capital of France?"})
        deps = c.get("/api/v1/health/dependencies").json()
    assert r.status_code == 200  # rate limiter fails open, cache degrades, health falls back locally
    assert r.json()["cache"]["status"] == "error"
    assert deps["redis"] == {"enabled": True, "reachable": False}
