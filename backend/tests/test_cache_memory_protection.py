"""Phase 11.1 (H1): the response cache can never exhaust Redis memory.

Global admission cap across all scopes and instances, a per-entry size limit, real deletion
on per-scope eviction, expiring metadata, bounded scope cardinality, recorded degradation -
and none of it may touch rate-limit, budget or health keys. Real Redis, DB 15, unique
`h1:cache_protection:<id>:` prefix (removed afterwards); no DB0, no PostgreSQL.
"""

import asyncio
import time
import uuid
from collections.abc import Iterator

import pytest
import redis as sync_redis
from fastapi.testclient import TestClient
from redis.exceptions import ResponseError

from app.budget.guard import RedisBudgetStore
from app.cache.semantic import SemanticCache, cache_scope, degraded_reason, query_hash
from app.embeddings.local import LocalHashingEmbedder
from app.infra.redis import RedisPoolExhaustedError
from app.main import create_app
from app.observability.metrics import Metrics
from app.ratelimit.limiter import RateLimiter
from app.routing.health_store import RedisHealthStore
from tests.conftest import TEST_HOST, TEST_REDIS_DB, redis_manager

PAYLOAD = {"answer": "cached answer", "model_used": "mock-fast"}
OOM = "OOM command not allowed when used memory > 'maxmemory'."


@pytest.fixture
def h1_prefix(redis_available) -> Iterator[str]:
    if not redis_available:
        pytest.skip("Redis not reachable on 127.0.0.1:6379 (start it with: docker compose up -d)")
    prefix = f"h1:cache_protection:{uuid.uuid4().hex[:10]}:"
    yield prefix
    client = sync_redis.Redis(host=TEST_HOST, port=6379, db=TEST_REDIS_DB)
    keys = list(client.scan_iter(match=prefix + "*", count=500))
    if keys:
        client.delete(*keys)


class RecordingMetrics:
    def __init__(self) -> None:
        self.degraded: list[tuple[str, str]] = []

    def cache_degraded(self, operation: str, reason: str) -> None:
        self.degraded.append((operation, reason))


def scope(n: int, **kw) -> str:
    base = dict(model_id=f"m{n}", provider="mock", provider_model=f"mock/m{n}", temperature=None,
                max_output_tokens=1024, mock_mode=True, embedding_id="local-hashing-v1-256")
    return cache_scope(**(base | kw))


async def with_cache(prefix, fn, **kw):
    m = redis_manager(prefix)
    await m.start()
    try:
        cache = SemanticCache(m, LocalHashingEmbedder(256), semantic_enabled=False, **kw)
        cache.metrics = RecordingMetrics()
        return await fn(cache)
    finally:
        await m.close()


async def put(cache: SemanticCache, sc: str, query: str, payload=PAYLOAD) -> bool:
    lookup = await cache.lookup(sc, query)
    try:
        return await cache.store(lookup, query, payload)
    finally:
        await cache.release(lookup)


async def entry_keys(cache: SemanticCache) -> list[bytes]:
    return [k async for k in cache.redis.client.scan_iter(match=cache.k("cache", "{*}", "e", "*"), count=500)]


# ---------------------------------------------------------------- 1. global cap across scopes
def test_global_cap_applies_across_scopes(h1_prefix):
    async def go(cache):
        stored = [await put(cache, scope(s), f"question {i} for scope {s}") for s in range(3) for i in range(3)]
        return (stored, await cache.redis.client.zcard(cache._global_index()), len(await entry_keys(cache)),
                cache.metrics.degraded)

    stored, indexed, entries, degraded = asyncio.run(with_cache(h1_prefix, go, max_entries=5))
    assert sum(stored) == 5 and stored[:5] == [True] * 5 and stored[5:] == [False] * 4
    assert indexed == 5 and entries == 5
    assert degraded == [("store", "global_cap")] * 4


# ---------------------------------------------------------------- 2. per-scope cap really frees memory
def test_per_scope_cap_still_enforced_and_evicted_answers_are_deleted(h1_prefix):
    async def go(cache):
        sc = scope(1)
        queries = [f"per scope question {i}" for i in range(5)]
        for q in queries:
            assert await put(cache, sc, q)
        client = cache.redis.client
        alive = [bool(await client.exists(cache._entry(sc, query_hash(q)))) for q in queries]
        return alive, await client.zcard(cache._zset(sc)), await client.zcard(cache._global_index())

    alive, scope_index, global_index = asyncio.run(with_cache(h1_prefix, go, max_entries_per_scope=3))
    assert alive == [False, False, True, True, True]  # oldest two evicted AND their answers deleted
    assert scope_index == 3
    assert global_index == 3  # evicted entries gave their global slots back


# ---------------------------------------------------------------- 3. expiry frees capacity; metadata expires
def test_expired_entries_free_global_capacity_and_metadata_has_ttls(h1_prefix):
    async def go(cache):
        assert all([await put(cache, scope(s), f"expiring {s}") for s in range(3)])
        assert not await put(cache, scope(9), "refused while full")
        client = cache.redis.client
        ttls = {name: await client.pttl(key) for name, key in {
            "global_index": cache._global_index(), "global_scopes": cache._global_scopes(),
            "scope_stats": cache._scope_stats(scope(0)), "scope_index": cache._zset(scope(0))}.items()}
        await asyncio.sleep(1.3)  # > ttl_seconds: every entry has expired
        admitted_after_expiry = await put(cache, scope(9), "admitted after expiry")
        members = await client.zrange(cache._global_index(), 0, -1)
        legacy_registry = await client.exists(cache.k("cache", "scopes"))
        return ttls, admitted_after_expiry, members, legacy_registry

    ttls, admitted, members, legacy = asyncio.run(with_cache(h1_prefix, go, ttl_seconds=1, max_entries=3))
    assert all(0 < ttl <= 61_000 for ttl in ttls.values()), ttls  # nothing grows without a bound
    assert admitted is True
    assert members == [f"{scope(9)}:{query_hash('admitted after expiry')}".encode()]  # expired members pruned
    assert legacy == 0  # the unbounded legacy SET is no longer written


# ---------------------------------------------------------------- 4. concurrent writers never exceed the cap
def test_concurrent_writes_never_exceed_global_cap(h1_prefix):
    async def go(cache):
        jobs = [put(cache, scope(i % 5), f"concurrent question {i}") for i in range(60)]
        stored = await asyncio.gather(*jobs)
        return sum(stored), await cache.redis.client.zcard(cache._global_index()), len(await entry_keys(cache))

    stored, indexed, entries = asyncio.run(with_cache(h1_prefix, go, max_entries=10))
    assert stored == 10 and indexed == 10 and entries == 10


# ---------------------------------------------------------------- 5. several app instances share one cap
def test_multiple_instances_share_one_global_cap_and_entries(h1_prefix):
    async def go():
        managers = [redis_manager(h1_prefix) for _ in range(3)]
        for m in managers:
            await m.start()
        try:
            caches = [SemanticCache(m, LocalHashingEmbedder(256), semantic_enabled=False, max_entries=6)
                      for m in managers]
            jobs = [put(caches[i % 3], scope(i % 4), f"instance question {i}") for i in range(30)]
            stored = sum(await asyncio.gather(*jobs))
            indexed = await caches[0].redis.client.zcard(caches[0]._global_index())
            assert await put(caches[0], scope(7), "shared answer") is False  # cap is global, not per instance
            await caches[1].redis.client.zrem(caches[1]._global_index(),
                                              *(await caches[1].redis.client.zrange(caches[1]._global_index(), 0, 0)))
            assert await put(caches[0], scope(7), "shared answer") is True
            other = await caches[2].lookup(scope(7), "shared answer")
            await caches[2].release(other)
            return stored, indexed, other.status
        finally:
            for m in managers:
                await m.close()

    stored, indexed, seen_by_other_instance = asyncio.run(go())
    assert stored == 6 and indexed == 6
    assert seen_by_other_instance == "hit_exact"


# ---------------------------------------------------------------- 6. bounded scope cardinality
def test_scope_normalisation_rules_and_parameter_correctness(h1_prefix):
    cache = SemanticCache(redis_manager(h1_prefix), LocalHashingEmbedder(256),
                          cacheable_max_output_tokens=frozenset({256, 512, 1024}))
    ok = cache.cacheable_parameters
    for t in (None, 0.0, 0.1, 0.7, 1.3, 2.0):
        assert ok(temperature=t, max_output_tokens=512), t
    for t in (0.75, 0.123, 1.05):
        assert not ok(temperature=t, max_output_tokens=512), t
    assert not ok(temperature=None, max_output_tokens=300)
    assert ok(temperature=None, max_output_tokens=300, always_cacheable_tokens=(300,))  # model's own maximum
    # scopes still use the EXACT values: different effective parameters never share answers
    assert len({scope(1, temperature=t) for t in (None, 0.0, 0.7, 0.8)}) == 4
    assert scope(1, max_output_tokens=512) != scope(1, max_output_tokens=1024)


def test_uncacheable_parameters_bypass_the_cache_end_to_end(infra_settings, h1_prefix):
    s = infra_settings(redis_key_prefix=h1_prefix, rate_limit_enabled=False, health_backend="memory")
    with TestClient(create_app(s)) as c:
        def chat(**body):
            r = c.post("/api/v1/chat", json={"query": "Explain Python variables", **body})
            assert r.status_code == 200
            return r.json()["cache"]["status"]

        off_grid = [chat(temperature=0.75), chat(temperature=0.75)]
        odd_tokens = [chat(max_output_tokens=300), chat(max_output_tokens=300)]
        on_grid = [chat(temperature=0.7), chat(temperature=0.7), chat(temperature=0.8)]
        default = [chat(), chat()]
        bypass_metric = c.app.state.container.metrics.cache_lookups.labels(status="bypass")._value.get()
    assert off_grid == ["bypass", "bypass"]  # answered normally, never cached
    assert odd_tokens == ["bypass", "bypass"]
    assert on_grid == ["miss", "hit_exact", "miss"]  # 0.8 is a different scope: no wrong answer
    assert default == ["miss", "hit_exact"]  # DEFAULT_MAX_OUTPUT_TOKENS is always cacheable
    assert bypass_metric == 4


# ---------------------------------------------------------------- 7 + 8. critical keys are never touched
def test_cache_eviction_and_clearing_never_touch_rate_limit_budget_or_health_keys(h1_prefix):
    async def go():
        m = redis_manager(h1_prefix)
        await m.start()
        try:
            client = m.client
            limiter = RateLimiter(m, per_minute=100, per_hour=0)
            for i in range(3):
                assert (await limiter.check_client(f"client-{i}")).allowed
            health = RedisHealthStore(m)
            await health.record_failure("mock-fast", "timeout")
            budget_keys = RedisBudgetStore(m)._keys("2026-11-11", "client-0")
            for key in budget_keys:
                await client.set(key, "123")
            critical = [k async for k in client.scan_iter(match=h1_prefix + "*", count=500)]
            snapshot = {k: (await client.type(k), await client.dump(k)) for k in critical}

            cache = SemanticCache(m, LocalHashingEmbedder(256), semantic_enabled=False, max_entries=4,
                                  max_entries_per_scope=2, ttl_seconds=1)
            for i in range(12):  # per-scope evictions + global refusals
                await put(cache, scope(i % 3), f"pressure question {i}")
            await asyncio.sleep(1.2)
            await put(cache, scope(0), "after expiry")  # expired-member pruning
            await cache.stats()  # stats-time pruning
            after_pressure = {k: (await client.type(k), await client.dump(k)) for k in critical}
            await cache.clear(reset_stats=True)
            after_clear = {k: (await client.type(k), await client.dump(k)) for k in critical}
            leftover_cache = [k async for k in client.scan_iter(match=cache.k("cache") + "*", count=500)]
            return critical, snapshot, after_pressure, after_clear, leftover_cache
        finally:
            await m.close()

    critical, snapshot, after_pressure, after_clear, leftover_cache = asyncio.run(go())
    names = b" ".join(critical)
    assert b":rl:client:" in names and b":health:mock-fast" in names and b":budget:" in names
    assert after_pressure == snapshot  # unchanged: same keys, types and values
    assert after_clear == snapshot
    assert leftover_cache == []


# ---------------------------------------------------------------- 9 + 10. full Redis: degrade, then recover
class _OOMClient:
    """Stands in for a Redis that refuses writes (maxmemory + noeviction): every call fails with OOM."""

    def __getattr__(self, name):
        async def refuse(*args, **kwargs):
            raise ResponseError(OOM)
        return refuse

    def pipeline(self, *args, **kwargs):
        raise ResponseError(OOM)


class _OOMManager:
    def __init__(self, real) -> None:
        self.keys = real.keys
        self.client = _OOMClient()


def test_redis_out_of_memory_degrades_gracefully_then_recovers(h1_prefix):
    async def go(cache):
        real = cache.redis
        cache.redis = _OOMManager(real)
        lookup = await cache.lookup(scope(1), "question during OOM")
        stored_during = await cache.store(lookup, "question during OOM", PAYLOAD)
        await cache.release(lookup)
        degraded = list(cache.metrics.degraded)
        cache.redis = real  # Redis writable again
        recovered_store = await put(cache, scope(1), "question during OOM")
        hit = await cache.lookup(scope(1), "question during OOM")
        await cache.release(hit)
        return lookup.status, stored_during, degraded, recovered_store, hit.status, hit.payload

    status, stored, degraded, recovered, hit_status, payload = asyncio.run(with_cache(h1_prefix, go))
    assert status == "error" and stored is False  # no exception reaches the request
    assert degraded == [("lookup", "oom"), ("store", "oom")]  # recorded, not silently a miss
    assert recovered is True and hit_status == "hit_exact" and payload["answer"] == PAYLOAD["answer"]


def test_rate_limit_stays_exact_while_the_cache_is_full(infra_settings, h1_prefix):
    s = infra_settings(redis_key_prefix=h1_prefix, rate_limit_enabled=True, rate_limit_per_minute=3,
                       rate_limit_per_hour=0, rate_limit_fail_open=False, cache_max_entries=1)
    with TestClient(create_app(s)) as c:
        codes, statuses = [], []
        for i in range(6):
            r = c.post("/api/v1/chat", json={"query": f"cache pressure question {i}"})
            codes.append(r.status_code)
            if r.status_code == 200:
                statuses.append(r.json()["cache"]["stored"])
        degraded = c.app.state.container.metrics.cache_degraded_total.labels(operation="store",
                                                                              reason="global_cap")._value.get()
    assert codes == [200, 200, 200, 429, 429, 429]  # limits enforced; cache refusals never bypass them
    assert statuses == [True, False, False]
    assert degraded == 2


def test_degradation_reasons_and_entry_size_limit(h1_prefix):
    assert degraded_reason(RedisPoolExhaustedError("busy")) == "pool_exhausted"
    assert degraded_reason(ResponseError(OOM)) == "oom"
    assert degraded_reason(ConnectionError("down")) == "redis_unavailable"

    async def go(cache):
        too_big = await put(cache, scope(1), "big answer", {"answer": "x" * 5000})
        small = await put(cache, scope(1), "small answer")
        return too_big, small, await cache.redis.client.zcard(cache._global_index()), cache.metrics.degraded

    too_big, small, indexed, degraded = asyncio.run(with_cache(h1_prefix, go, max_entry_bytes=2048))
    assert too_big is False and small is True and indexed == 1  # an oversized answer takes no slot
    assert degraded == [("store", "entry_too_large")]
    m = Metrics(enabled=True, model_ids=[], providers=set(), app_version="t", mock_mode=True)
    m.cache_degraded("store", "something-unexpected")  # labels stay bounded
    assert m.cache_degraded_total.labels(operation="store", reason="other")._value.get() == 1


# ---------------------------------------------------------------- 11. hit/miss behaviour unchanged
def test_hit_miss_and_stats_behaviour_unchanged(h1_prefix):
    async def go(cache):
        first = await cache.lookup(scope(1), "Explain Python variables")
        assert first.status == "miss" and first.lock_token
        assert await cache.store(first, "Explain Python variables", PAYLOAD)
        await cache.release(first)
        second = await cache.lookup(scope(1), "Explain Python variables")
        await cache.release(second)
        stats = await cache.stats()
        return second, stats

    second, stats = asyncio.run(with_cache(h1_prefix, go))
    assert second.status == "hit_exact" and second.payload["answer"] == PAYLOAD["answer"]
    assert (stats.hits_exact, stats.misses, stats.stores, stats.total_entries, stats.scopes) == (1, 1, 1, 1, 1)
    assert any("Global cap" in note for note in stats.notes)


# ---------------------------------------------------------------- 12. isolation + deterministic fingerprints
def test_scope_isolation_and_deterministic_fingerprints(h1_prefix):
    assert scope(1, temperature=0.7) == scope(1, temperature=0.7)  # deterministic
    assert query_hash("What is JavaScript?") == query_hash("  What   is JavaScript?  ")  # whitespace only
    assert query_hash("What is JavaScript?") != query_hash("What is javascript?")  # case preserved

    async def go(cache):
        assert await put(cache, scope(1), "isolated question", {"answer": "answer for model 1"})
        other = await cache.lookup(scope(2), "isolated question")
        await cache.release(other)
        same = await cache.lookup(scope(1), "isolated question")
        await cache.release(same)
        return other.status, same.payload["answer"]

    other_status, same_answer = asyncio.run(with_cache(h1_prefix, go))
    assert other_status == "miss"  # another scope never sees this answer
    assert same_answer == "answer for model 1"


def test_admission_is_fast_relative_to_bound(h1_prefix):
    """Sanity: pruning keeps the global index at the cap even after many refused writes (no growth)."""
    async def go(cache):
        t0 = time.perf_counter()
        for i in range(200):
            await put(cache, scope(i % 20), f"burst {i}")
        return await cache.redis.client.zcard(cache._global_index()), time.perf_counter() - t0

    indexed, _elapsed = asyncio.run(with_cache(h1_prefix, go, max_entries=25))
    assert indexed == 25
