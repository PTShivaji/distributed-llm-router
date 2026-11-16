"""Distributed rate limiting (real Redis, isolated namespace) + client identity."""

import asyncio
from types import SimpleNamespace

import pytest

from app.infra.redis import RedisManager
from app.ratelimit.limiter import ClientIdentityResolver, RateLimiter, RateLimitResult, Window, hash_subject
from tests.conftest import TEST_REDIS_DB, model, redis_manager


async def with_limiter(prefix, fn, n_instances=1, **kw):
    managers = [redis_manager(prefix) for _ in range(n_instances)]
    for m in managers:
        await m.start()
    try:
        return await fn(*[RateLimiter(m, **kw) for m in managers])
    finally:
        for m in managers:
            await m.close()


def test_limit_enforced_with_headers(redis_prefix):
    async def go(rl):
        results = [await rl.check_client("10.0.0.1") for _ in range(4)]
        return results

    results = asyncio.run(with_limiter(redis_prefix, go, per_minute=3, per_hour=100))
    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results[:3]] == [2, 1, 0]
    h = results[3].headers()
    assert h["X-RateLimit-Limit"] == "3" and h["X-RateLimit-Remaining"] == "0"
    assert h["X-RateLimit-Window"] == "minute" and 1 <= int(h["Retry-After"]) <= 60
    assert "Retry-After" not in results[0].headers()


def test_clients_are_independent(redis_prefix):
    async def go(rl):
        await rl.check_client("a")
        return (await rl.check_client("a")).allowed, (await rl.check_client("b")).allowed

    assert asyncio.run(with_limiter(redis_prefix, go, per_minute=1, per_hour=0)) == (False, True)


def test_hour_window_enforced_independently(redis_prefix):
    async def go(rl):
        return [await rl.check_client("c") for _ in range(3)]

    results = asyncio.run(with_limiter(redis_prefix, go, per_minute=100, per_hour=2))
    assert [r.allowed for r in results] == [True, True, False]
    assert results[2].window == "hour" and results[2].reset_seconds > 60


def test_rejected_request_consumes_nothing(redis_prefix):
    async def go(rl):
        windows = [Window("minute", 1, 60), Window("hour", 10, 3600)]
        await rl.check("client", "s", windows)
        for _ in range(5):
            await rl.check("client", "s", windows)
        return int(await rl.redis.client.get(rl.redis.keys("rl", "client", "s", "hour")))

    assert asyncio.run(with_limiter(redis_prefix, go)) == 1


def test_concurrent_requests_across_instances_are_exact(redis_prefix):
    async def go(a, b, c):
        results = await asyncio.gather(*[(a, b, c)[i % 3].check_client("shared-client") for i in range(40)])
        return sum(r.allowed for r in results)

    assert asyncio.run(with_limiter(redis_prefix, go, n_instances=3, per_minute=17, per_hour=1000)) == 17


def test_window_expires(redis_prefix):
    async def go(rl):
        w = [Window("minute", 1, 1)]
        first = await rl.check("client", "x", w)
        second = await rl.check("client", "x", w)
        await asyncio.sleep(1.1)
        third = await rl.check("client", "x", w)
        return first.allowed, second.allowed, third.allowed

    assert asyncio.run(with_limiter(redis_prefix, go)) == (True, False, True)


def test_keys_do_not_contain_raw_ip(redis_prefix):
    async def go(rl):
        await rl.check_client("203.0.113.9")
        return [k async for k in rl.redis.client.scan_iter(match=redis_prefix + "*")]

    keys = asyncio.run(with_limiter(redis_prefix, go))
    assert keys and all(b"203.0.113.9" not in k for k in keys)
    assert hash_subject("203.0.113.9") in keys[0].decode()


def test_zero_limits_mean_unlimited(redis_prefix):
    async def go(rl):
        return await rl.check_client("x")

    r = asyncio.run(with_limiter(redis_prefix, go, per_minute=0, per_hour=0))
    assert r.allowed and r.headers() == {}


@pytest.mark.parametrize("fail_open", [True, False])
def test_redis_outage_fail_open_or_closed(fail_open):
    async def go():
        m = RedisManager(f"redis://localhost:1/{TEST_REDIS_DB}", connect_timeout=0.2, socket_timeout=0.2)
        await m.start()
        r = await RateLimiter(m, fail_open=fail_open).check_client("x")
        await m.close()
        return r

    r = asyncio.run(go())
    assert r.degraded and r.allowed is fail_open


def test_model_rate_limit_triggers_fallback(redis_prefix, make_service):
    from app.routing.service import ChatCommand

    async def go():
        m = redis_manager(redis_prefix)
        await m.start()
        try:
            svc = make_service([model("fast", fallbacks=("backup",)), model("backup", priority=500)])
            svc.model_limiter = RateLimiter(m, model_limits={"fast": 2})
            outs = [await svc.chat(ChatCommand("What is the capital of France?", f"r{i}")) for i in range(3)]
            return outs
        finally:
            await m.close()

    outs = asyncio.run(go())
    assert [o.model_used for o in outs] == ["fast", "fast", "backup"]
    assert outs[2].fallback.attempts[0].outcome == "skipped_rate_limited"


# --- Client identity ------------------------------------------------------------
def req(peer: str, xff: str | None = None):
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_forwarded_header_ignored_by_default():
    assert ClientIdentityResolver().resolve(req("10.0.0.5", "1.2.3.4")) == "10.0.0.5"


def test_forwarded_header_ignored_from_untrusted_peer():
    r = ClientIdentityResolver(trust_proxy_headers=True, trusted_proxies=["10.0.0.0/8"])
    assert r.resolve(req("198.51.100.7", "1.2.3.4")) == "198.51.100.7"


def test_trusted_proxy_uses_rightmost_untrusted_hop():
    r = ClientIdentityResolver(trust_proxy_headers=True, trusted_proxies=["10.0.0.0/8"])
    # client spoofs "6.6.6.6" at the left; the real client is the right-most untrusted hop
    assert r.resolve(req("10.0.0.2", "6.6.6.6, 203.0.113.9, 10.0.0.3")) == "203.0.113.9"


def test_unlimited_result_has_no_headers():
    assert RateLimitResult(True, None, None, None, None).headers() == {}
