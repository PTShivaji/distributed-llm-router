"""Redis-backed health: shared state, atomicity, cooldown, probes, outages."""

import asyncio
import logging

from app.infra.redis import RedisManager
from app.routing.health import HealthState
from app.routing.health_store import LocalHealthStore, RedisHealthStore, compute_state
from tests.conftest import TEST_REDIS_DB, redis_manager


def test_compute_state_rules():
    assert compute_state(0, 0, 100, 3, 30) == (HealthState.HEALTHY, False)
    assert compute_state(2, 99, 100, 3, 30) == (HealthState.DEGRADED, False)
    assert compute_state(3, 99, 100, 3, 30) == (HealthState.UNHEALTHY, False)
    assert compute_state(3, 50, 100, 3, 30) == (HealthState.DEGRADED, True)  # cooled down -> probe


async def two_instances(prefix, fn, **kw):
    a, b = redis_manager(prefix), redis_manager(prefix)
    await a.start()
    await b.start()
    try:
        return await fn(RedisHealthStore(a, **kw), RedisHealthStore(b, **kw))
    finally:
        await a.close()
        await b.close()


def test_health_is_shared_between_instances(redis_prefix):
    async def go(a, b):
        await a.record_failure("m", "timeout")
        await a.record_failure("m", "unavailable")
        view = await b.view(["m", "other"])
        assert view.state("m") == HealthState.UNHEALTHY and not view.is_available("m")
        snap = view.snapshot("m")
        assert (snap.consecutive_failures, snap.total_failures, snap.last_error_kind) == (2, 2, "unavailable")
        assert view.state("other") == HealthState.HEALTHY
        await b.record_success("m")
        snap = (await a.view(["m"])).snapshot("m")
        assert snap.state == HealthState.HEALTHY and snap.total_successes == 1 and snap.total_failures == 2

    asyncio.run(two_instances(redis_prefix, go, failure_threshold=2, cooldown_seconds=60))


def test_concurrent_updates_are_atomic(redis_prefix):
    async def go(a, b):
        await asyncio.gather(*[(a if i % 2 else b).record_failure("m", "rate_limit") for i in range(60)])
        await asyncio.gather(*[(a if i % 2 else b).record_success("n") for i in range(40)])
        view = await a.view(["m", "n"])
        assert view.snapshot("m").total_failures == 60 and view.snapshot("m").consecutive_failures == 60
        assert view.snapshot("n").total_successes == 40

    asyncio.run(two_instances(redis_prefix, go))


def test_cooldown_then_single_recovery_probe(redis_prefix):
    async def go(a, b):
        await a.record_failure("m", "timeout")
        assert not (await b.view(["m"])).is_available("m")
        await asyncio.sleep(0.4)
        view = await b.view(["m"])
        assert view.is_available("m") and view.needs_probe("m")
        results = await asyncio.gather(a.acquire_probe("m"), b.acquire_probe("m"), a.acquire_probe("m"))
        assert sorted(results) == [False, False, True]  # exactly one instance probes
        await b.record_success("m")  # probe succeeded -> closed circuit, lock released
        view = await a.view(["m"])
        assert view.state("m") == HealthState.HEALTHY and not view.needs_probe("m")

    asyncio.run(two_instances(redis_prefix, go, failure_threshold=1, cooldown_seconds=0.3))


def test_failed_probe_reopens_circuit(redis_prefix):
    async def go(a, b):
        await a.record_failure("m", "timeout")
        await asyncio.sleep(0.35)
        assert await a.acquire_probe("m")
        await a.record_failure("m", "timeout")  # probe failed
        assert (await b.view(["m"])).state("m") == HealthState.UNHEALTHY

    asyncio.run(two_instances(redis_prefix, go, failure_threshold=1, cooldown_seconds=0.3))


def test_state_change_events_logged(redis_prefix, caplog):
    async def go(a, b):
        with caplog.at_level(logging.INFO, logger="app.routing.health"):
            await a.record_failure("m", "timeout")
            await a.record_failure("m", "timeout")
            await a.record_success("m")

    asyncio.run(two_instances(redis_prefix, go, failure_threshold=2, cooldown_seconds=60))
    changes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("health.state_change")]
    assert len(changes) == 3
    assert "to_state=unhealthy" in changes[1] and "to_state=healthy" in changes[2]


def test_health_keys_have_ttl(redis_prefix):
    async def go(a, b):
        await a.record_failure("m", "timeout")
        assert 0 < await a.redis.client.ttl(a._key("m")) <= 120

    asyncio.run(two_instances(redis_prefix, go, key_ttl_seconds=120))


def test_redis_outage_falls_back_to_local(caplog):
    async def go():
        m = RedisManager(f"redis://localhost:1/{TEST_REDIS_DB}", connect_timeout=0.2, socket_timeout=0.2)
        await m.start()
        store = RedisHealthStore(m, failure_threshold=1, cooldown_seconds=60)
        with caplog.at_level(logging.WARNING):
            await store.record_failure("m", "timeout")
            view = await store.view(["m"])
        assert store.degraded
        assert view.state("m") == HealthState.UNHEALTHY  # tracked locally during the outage
        assert await store.acquire_probe("m") is True
        await m.close()

    asyncio.run(go())
    assert sum("health.redis_unavailable" in r.getMessage() for r in caplog.records) == 1  # logged once


def test_local_store_matches_phase3_semantics():
    async def go():
        s = LocalHealthStore()
        await s.record_failure("m", "x")
        view = await s.view(["m"])
        return view.state("m"), view.needs_probe("m"), await s.acquire_probe("m")

    assert asyncio.run(go()) == (HealthState.DEGRADED, False, True)


def test_chat_service_uses_shared_health(redis_prefix, make_service):
    """Instance A's failures make instance B route around the model."""
    from app.providers.mock import MockProvider
    from app.routing.service import ChatCommand
    from tests.conftest import model

    async def go():
        ma, mb = redis_manager(redis_prefix), redis_manager(redis_prefix)
        await ma.start()
        await mb.start()
        try:
            ms = [model("primary", priority=1), model("secondary", priority=2)]
            a = make_service(ms, provider=MockProvider({"primary": "unavailable"}), max_retries=0,
                             health=None, fallback_enabled=False)
            b = make_service(ms, provider=MockProvider())
            a.health = RedisHealthStore(ma, failure_threshold=1, cooldown_seconds=60)
            b.health = RedisHealthStore(mb, failure_threshold=1, cooldown_seconds=60)
            try:
                await a.chat(ChatCommand("What is the capital of France?", "r1"))
            except Exception:
                pass
            out = await b.chat(ChatCommand("What is the capital of France?", "r2"))
            return out.selected_model
        finally:
            await ma.close()
            await mb.close()

    assert asyncio.run(go()) == "secondary"
