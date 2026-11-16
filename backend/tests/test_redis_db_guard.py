"""Phase 11.3 H3.2: the test suite can only ever use Redis logical DB 15.

The conftest guard checks every redis-py connection (sync and asyncio) when it connects,
before any network I/O, and refuses database switches on open connections. Every guard
test below targets 127.0.0.1:1 or a non-existent unix socket, so even a broken guard could
never reach a real Redis server, let alone DB 0.
"""

import asyncio
from contextlib import contextmanager

import pytest
import redis
import redis.asyncio as aioredis

from app.infra.redis import RedisManager
from tests import conftest as cf

DEAD = dict(host="127.0.0.1", port=1, socket_connect_timeout=0.3, socket_timeout=0.3)


@contextmanager
def expect_blocked(count: int):
    """Asserts the guard blocked at least `count` accesses, then clears them (so the
    autouse `_no_forbidden_redis_access` check does not fail this test). "At least":
    redis-py's retry policy may re-attempt a refused connect; each retry is refused too."""
    before = len(cf.REDIS_BLOCKED_ACCESS)
    try:
        yield
    finally:
        got = cf.REDIS_BLOCKED_ACCESS[before:]
        del cf.REDIS_BLOCKED_ACCESS[before:]
    assert len(got) >= count, got


def test_blocked_access_looks_like_an_unreachable_server_to_application_code():
    assert issubclass(cf.ForbiddenRedisDatabase, redis.exceptions.ConnectionError)


def test_guard_is_installed_on_sync_and_async_entry_points():
    import redis.asyncio.client as async_client
    import redis.asyncio.connection as async_connection
    import redis.client as sync_client
    import redis.connection as sync_connection

    for fn in (sync_connection.AbstractConnection.connect_check_health,
               async_connection.AbstractConnection.connect_check_health,
               sync_client.Redis.execute_command, async_client.Redis.execute_command):
        assert getattr(fn, "_llmr_guard", False)


@pytest.mark.parametrize("db", [0, 1, 14, "0"])
def test_sync_connection_to_other_database_is_refused(db):
    client = redis.Redis(db=db, **DEAD)
    with expect_blocked(1), pytest.raises(cf.ForbiddenRedisDatabase):
        client.ping()


def test_default_database_and_url_forms_are_refused():
    with expect_blocked(3):
        with pytest.raises(cf.ForbiddenRedisDatabase):
            redis.Redis(**DEAD).ping()  # redis-py default is DB 0
        with pytest.raises(cf.ForbiddenRedisDatabase):
            redis.Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.3).ping()
        with pytest.raises(cf.ForbiddenRedisDatabase):
            redis.Redis.from_url("redis://127.0.0.1:1", socket_connect_timeout=0.3).ping()


def test_unix_socket_connection_to_db0_is_refused():
    client = redis.Redis(unix_socket_path="/nonexistent/llmr-test.sock", db=0)
    with expect_blocked(1), pytest.raises(cf.ForbiddenRedisDatabase):
        client.ping()


def test_async_connection_to_db0_is_refused():
    async def go():
        client = aioredis.Redis(db=0, **DEAD)
        try:
            await client.ping()
        finally:
            await client.aclose()

    with expect_blocked(1), pytest.raises(cf.ForbiddenRedisDatabase):
        asyncio.run(go())


def test_database_switch_commands_are_refused():
    client = redis.Redis(db=cf.TEST_REDIS_DB, **DEAD)
    with expect_blocked(3):
        with pytest.raises(cf.ForbiddenRedisDatabase):
            client.execute_command("SELECT", 0)
        with pytest.raises(cf.ForbiddenRedisDatabase):
            client.swapdb(0, 15)
        with pytest.raises(cf.ForbiddenRedisDatabase):
            client.move("some-key", 0)


def test_async_database_switch_is_refused():
    async def go():
        client = aioredis.Redis(db=cf.TEST_REDIS_DB, **DEAD)
        try:
            await client.execute_command(b"select", b"0")
        finally:
            await client.aclose()

    with expect_blocked(1), pytest.raises(cf.ForbiddenRedisDatabase):
        asyncio.run(go())


def test_db15_passes_the_guard():
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    before = len(cf.REDIS_BLOCKED_ACCESS)
    network_errors = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)
    no_retry = dict(DEAD, retry=Retry(NoBackoff(), 0))  # one real (failing) attempt; keeps the test fast
    with pytest.raises(network_errors) as info:  # refused/timed out by the network, not the guard
        redis.Redis(db=cf.TEST_REDIS_DB, **no_retry).ping()
    assert not isinstance(info.value, cf.ForbiddenRedisDatabase)
    with pytest.raises(network_errors) as info:
        redis.Redis(db=cf.TEST_REDIS_DB, **no_retry).move("some-key", cf.TEST_REDIS_DB)
    assert not isinstance(info.value, cf.ForbiddenRedisDatabase)
    assert len(cf.REDIS_BLOCKED_ACCESS) == before


def test_application_redis_manager_on_db0_is_caught_even_if_swallowed():
    """RedisManager.start() never raises; the attempt must still be recorded and reported."""
    async def go():
        m = RedisManager("redis://127.0.0.1:1/0", connect_timeout=0.3, socket_timeout=0.3)
        try:
            return await m.start()
        finally:
            await m.close()

    before = len(cf.REDIS_BLOCKED_ACCESS)
    assert asyncio.run(go()) is False
    assert len(cf.REDIS_BLOCKED_ACCESS) > before
    with pytest.raises(pytest.fail.Exception, match="forbidden Redis access"):
        cf._fail_on_redis_blocked_since(before)
    assert len(cf.REDIS_BLOCKED_ACCESS) == before  # cleared after reporting


def test_real_redis_db15_works_through_the_guard(redis_prefix):
    client = redis.Redis(host=cf.TEST_HOST, port=6379, db=cf.TEST_REDIS_DB)
    key = redis_prefix + "guard-check"
    assert client.set(key, "1", ex=30) and client.get(key) == b"1"
    assert client.connection_pool.connection_kwargs["db"] == cf.TEST_REDIS_DB
