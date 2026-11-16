"""SemanticCache against real Redis (isolated namespace)."""

import asyncio

import pytest

from app.cache.semantic import SemanticCache, cache_scope, normalize_query, query_hash
from app.embeddings.base import EmbeddingError, EmbeddingProvider
from app.embeddings.local import LocalHashingEmbedder
from app.infra.redis import RedisManager
from tests.conftest import TEST_REDIS_DB, redis_manager

SCOPE_A = cache_scope(model_id="m1", provider="mock", provider_model="mock/m1", temperature=None,
                      max_output_tokens=1024, mock_mode=True, embedding_id="local-hashing-v1-256")
PAYLOAD = {"answer": "Variables are names bound to values.", "model_used": "m1", "usage": {"total_tokens": 10}}


async def with_cache(prefix, fn, **kw):
    m = redis_manager(prefix)
    await m.start()
    # These tests exercise the semantic machinery with the LEXICAL embedder, which is
    # gated off by default since step 6A: opt in explicitly (assertions unchanged).
    kw.setdefault("allow_lexical", True)
    try:
        return await fn(SemanticCache(m, LocalHashingEmbedder(256), **kw))
    finally:
        await m.close()


async def miss_then_store(cache, query, scope=SCOPE_A, payload=PAYLOAD):
    lk = await cache.lookup(scope, query)
    assert lk.status == "miss"
    assert await cache.store(lk, query, payload)
    await cache.release(lk)
    return lk


def test_scope_depends_on_configuration():
    base = dict(model_id="m1", provider="mock", provider_model="mock/m1", temperature=None,
                max_output_tokens=1024, mock_mode=True, embedding_id="e")
    assert cache_scope(**base) == cache_scope(**base)
    for change in [{"model_id": "m2"}, {"provider": "openai"}, {"provider_model": "x"}, {"temperature": 0.7},
                   {"max_output_tokens": 10}, {"mock_mode": False}, {"embedding_id": "other"}]:
        assert cache_scope(**{**base, **change}) != cache_scope(**base), change


def test_exact_normalisation():
    assert normalize_query("  Explain   Python\nvariables ") == "Explain Python variables"
    assert query_hash("Explain Python variables") == query_hash(" Explain  Python variables ")
    assert query_hash("explain python variables") != query_hash("Explain Python variables")  # case kept


def test_miss_store_exact_hit(redis_prefix):
    async def go(cache):
        await miss_then_store(cache, "Explain Python variables")
        hit = await cache.lookup(SCOPE_A, "Explain  Python variables ")
        assert hit.status == "hit_exact" and hit.similarity == 1.0
        assert hit.payload["answer"] == PAYLOAD["answer"]
        assert "cached_at" in hit.payload

    asyncio.run(with_cache(redis_prefix, go))


def test_semantic_hit_above_threshold(redis_prefix):
    async def go(cache):
        await miss_then_store(cache, "Explain Python variables")
        hit = await cache.lookup(SCOPE_A, "explain python variables?")
        assert hit.status == "hit_semantic" and hit.similarity >= 0.92

    asyncio.run(with_cache(redis_prefix, go))


def test_below_threshold_is_miss_and_reports_best_similarity(redis_prefix):
    async def go(cache):
        await miss_then_store(cache, "Explain Python variables")
        lk = await cache.lookup(SCOPE_A, "Explain JavaScript variables")
        assert lk.status == "miss" and lk.similarity is not None and lk.similarity < 0.92
        await cache.release(lk)

    asyncio.run(with_cache(redis_prefix, go))


@pytest.mark.parametrize("threshold, expected", [(0.5, "hit_semantic"), (0.95, "miss")])
def test_threshold_is_configurable(redis_prefix, threshold, expected):
    async def go(cache):
        await miss_then_store(cache, "Explain Python variables")
        lk = await cache.lookup(SCOPE_A, "Explain Python variables please")  # similarity ~0.88
        await cache.release(lk)
        return lk.status

    assert asyncio.run(with_cache(redis_prefix, go, similarity_threshold=threshold)) == expected


def test_scopes_are_isolated(redis_prefix):
    other = cache_scope(model_id="m2", provider="mock", provider_model="mock/m2", temperature=None,
                        max_output_tokens=1024, mock_mode=True, embedding_id="local-hashing-v1-256")

    async def go(cache):
        await miss_then_store(cache, "Explain Python variables")
        lk = await cache.lookup(other, "Explain Python variables")
        assert lk.status == "miss"
        await cache.release(lk)

    asyncio.run(with_cache(redis_prefix, go))


def test_semantic_disabled_means_exact_only(redis_prefix):
    async def go(cache):
        await miss_then_store(cache, "Explain Python variables")
        assert (await cache.lookup(SCOPE_A, "Explain Python variables")).status == "hit_exact"
        lk = await cache.lookup(SCOPE_A, "explain python variables?")
        assert lk.status == "miss" and lk.embedding is None
        await cache.release(lk)

    asyncio.run(with_cache(redis_prefix, go, semantic_enabled=False))


def test_ttl_expiry(redis_prefix):
    async def go(cache):
        await miss_then_store(cache, "Explain Python variables")
        assert (await cache.lookup(SCOPE_A, "Explain Python variables")).hit
        await asyncio.sleep(1.3)
        for q in ["Explain Python variables", "explain python variables?"]:
            lk = await cache.lookup(SCOPE_A, q)
            assert lk.status == "miss", q
            await cache.release(lk)

    asyncio.run(with_cache(redis_prefix, go, ttl_seconds=1))


def test_max_entries_per_scope_evicts_oldest(redis_prefix):
    async def go(cache):
        for i in range(5):
            await miss_then_store(cache, f"distinct topic number {i} about astronomy {i * 7}")
        assert await cache.redis.client.zcard(cache._zset(SCOPE_A)) == 3
        assert await cache.redis.client.hlen(cache._vectors(SCOPE_A)) == 3

    asyncio.run(with_cache(redis_prefix, go, max_entries_per_scope=3))


def test_clear_invalidates_everything_but_keeps_stats(redis_prefix):
    async def go(cache):
        await miss_then_store(cache, "Explain Python variables")
        deleted = await cache.clear()
        assert deleted > 0
        lk = await cache.lookup(SCOPE_A, "Explain Python variables")
        assert lk.status == "miss"
        await cache.release(lk)
        s = await cache.stats()
        assert s.misses == 2 and s.stores == 1  # stats preserved
        await cache.clear(reset_stats=True)
        assert (await cache.stats()).misses == 0

    asyncio.run(with_cache(redis_prefix, go))


def test_stats(redis_prefix):
    async def go(cache):
        await miss_then_store(cache, "Explain Python variables")
        await cache.lookup(SCOPE_A, "Explain Python variables")
        await cache.lookup(SCOPE_A, "explain python variables?")
        s = await cache.stats()
        assert (s.hits_exact, s.hits_semantic, s.misses, s.stores) == (1, 1, 1, 1)
        assert s.hits == 2 and s.hit_rate == pytest.approx(2 / 3, abs=1e-3)
        assert s.total_entries == 1 and s.scopes == 1 and s.available
        assert s.approx_storage_bytes and s.approx_storage_bytes > 0
        assert not s.embedding_is_semantic and any("lexical" in n for n in s.notes)

    asyncio.run(with_cache(redis_prefix, go))


def test_prompt_text_is_never_stored(redis_prefix):
    secret_query = "My account number is 99887766, explain compound interest"

    async def go(cache):
        await miss_then_store(cache, secret_query)
        client = cache.redis.client
        for key in [k async for k in client.scan_iter(match=redis_prefix + "*")]:
            if await client.type(key) == b"string":
                assert b"99887766" not in await client.get(key)
            assert b"99887766" not in key

    asyncio.run(with_cache(redis_prefix, go))


def test_stampede_lock_waiters_get_the_stored_result(redis_prefix):
    async def go(cache):
        first = await cache.lookup(SCOPE_A, "Explain Python variables")
        assert first.status == "miss" and first.lock_token

        async def store_later():
            await asyncio.sleep(0.2)
            await cache.store(first, "Explain Python variables", PAYLOAD)
            await cache.release(first)

        waiters = asyncio.gather(*[cache.lookup(SCOPE_A, "Explain Python variables") for _ in range(5)])
        results, _ = await asyncio.gather(waiters, store_later())
        assert all(r.status == "hit_exact" for r in results)
        assert all(r.lock_token is None for r in results)

    asyncio.run(with_cache(redis_prefix, go, semantic_enabled=False, lock_wait_seconds=3))


def test_lock_wait_gives_up_without_blocking_forever(redis_prefix):
    async def go(cache):
        holder = await cache.lookup(SCOPE_A, "Q")
        assert holder.lock_token
        lk = await cache.lookup(SCOPE_A, "Q")  # holder never stores
        assert lk.status == "miss" and lk.lock_token is None and lk.latency_ms >= 200

    asyncio.run(with_cache(redis_prefix, go, semantic_enabled=False, lock_wait_seconds=0.3))


def test_redis_outage_reports_error_status():
    async def go():
        m = RedisManager(f"redis://localhost:1/{TEST_REDIS_DB}", connect_timeout=0.2, socket_timeout=0.2)
        await m.start()
        cache = SemanticCache(m, LocalHashingEmbedder(64))
        lk = await cache.lookup(SCOPE_A, "anything")
        assert lk.status == "error" and lk.error
        assert await cache.store(lk, "anything", PAYLOAD) is False
        s = await cache.stats()
        assert not s.available and s.total_entries is None
        await m.close()

    asyncio.run(go())


def test_embedding_failure_degrades_to_miss(redis_prefix):
    class Broken(EmbeddingProvider):
        id, dimensions, is_semantic = "broken", 8, False

        async def embed(self, text):
            raise EmbeddingError("down")

    async def go():
        m = redis_manager(redis_prefix)
        await m.start()
        lk = await SemanticCache(m, Broken(), allow_lexical=True).lookup(SCOPE_A, "q")  # opt in (step 6A)
        await m.close()
        return lk

    lk = asyncio.run(go())
    assert lk.status == "miss" and lk.error == "embedding_failed"
