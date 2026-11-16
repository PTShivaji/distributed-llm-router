"""Phase 5 step 6B: behaviour-preservation tests for the measured optimizations."""

import math
import random

import pytest
from opentelemetry import trace

from app.embeddings.base import cosine_similarity, from_bytes, normalize, to_bytes
from app.embeddings.local import LocalHashingEmbedder
from app.observability.tracing import NOOP_TRACER, Tracing, start_span


# --- Optimization 1: cosine via math.sumprod ---------------------------------
def _reference_dot(a, b):
    return sum(x * y for x, y in zip(a, b))  # the pre-6B implementation


def test_sumprod_cosine_matches_reference_implementation():
    rng = random.Random(1234)
    e = LocalHashingEmbedder(256)
    worst = 0.0
    for i in range(2000):
        a = normalize([rng.uniform(-1, 1) for _ in range(256)])
        b = from_bytes(to_bytes(normalize([rng.uniform(-1, 1) for _ in range(256)])))
        worst = max(worst, abs(cosine_similarity(a, b) - _reference_dot(a, b)))
        q, d = e.embed_sync(f"query {i}"), from_bytes(to_bytes(e.embed_sync(f"doc {i % 17}")))
        worst = max(worst, abs(cosine_similarity(q, d) - _reference_dot(q, d)))
    assert worst < 1e-12


def test_sumprod_cosine_edge_cases():
    assert cosine_similarity([0.0] * 4, [1.0] * 4) == 0.0
    assert math.isclose(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)
    with pytest.raises(ValueError):
        cosine_similarity([1.0], [1.0, 2.0])


def test_threshold_decisions_unchanged_on_boundary_pairs():
    """Hit/miss decisions at 0.92 are identical to the reference for measured pairs."""
    e = LocalHashingEmbedder(256)
    pairs = [("Explain Python variables", "Explain Python variables please"),
             ("Explain Python variables", "explain python variables!"),
             ("How do I reverse a list in Python?", "How can I reverse a list in Python?")]
    for a, b in pairs:
        va, vb = e.embed_sync(a), from_bytes(to_bytes(e.embed_sync(b)))
        assert (cosine_similarity(va, vb) >= 0.92) == (_reference_dot(va, vb) >= 0.92)


# --- Optimization 3: tracing short-circuit when disabled ---------------------
def test_disabled_tracing_yields_invalid_span_and_never_swallows_errors():
    with start_span(NOOP_TRACER, "x", **{"llm.model": "m"}) as span:
        assert span is trace.INVALID_SPAN and not span.is_recording()
    with pytest.raises(KeyError):
        with start_span(NOOP_TRACER, "y"):
            raise KeyError("must propagate")


def test_disabled_tracing_does_not_sanitize_attributes(monkeypatch):
    import app.observability.tracing as T

    calls = []
    monkeypatch.setattr(T, "safe_attributes", lambda attrs: calls.append(attrs) or {})
    with start_span(NOOP_TRACER, "x", **{"llm.model": "m"}):
        pass
    assert calls == []


def test_enabled_tracing_unchanged():
    t = Tracing(enabled=True, exporter="memory")
    with start_span(t.tracer, "outer", **{"llm.model": "m", "not.allowed": "x"}) as outer:
        assert outer.is_recording()
        with start_span(t.tracer, "inner"):
            pass
    with pytest.raises(RuntimeError):
        with start_span(t.tracer, "boom"):
            raise RuntimeError("secret message")
    spans = {s.name: s for s in t.memory_exporter.get_finished_spans()}
    assert spans["inner"].parent.span_id == spans["outer"].context.span_id
    assert dict(spans["outer"].attributes) == {"llm.model": "m"}  # sanitisation still applied
    assert spans["boom"].status.description == "RuntimeError" and not spans["boom"].events
    t.shutdown()


def test_disabled_inner_span_keeps_outer_trace_context_for_logs():
    t = Tracing(enabled=True, exporter="memory")
    with start_span(t.tracer, "outer") as outer:
        with start_span(NOOP_TRACER, "inner-disabled"):
            assert trace.get_current_span() is outer  # no invalid span attached
    t.shutdown()


# --- Optimization 4: fewer Redis round trips (behaviour must be identical) ----
import asyncio  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from redis.asyncio.client import Pipeline, Redis  # noqa: E402

from app.cache.semantic import SemanticCache, cache_scope  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routing.health import HealthState  # noqa: E402
from tests.conftest import redis_manager  # noqa: E402

SCOPE = cache_scope(model_id="m", provider="mock", provider_model="mock/m", temperature=None,
                    max_output_tokens=1024, mock_mode=True, embedding_id="local-hashing-v1-256")


@pytest.fixture
def round_trips(monkeypatch):
    """Count Redis round trips (single commands + whole pipelines)."""
    counter = {"n": 0}
    orig_exec, orig_pipe = Redis.execute_command, Pipeline.execute

    async def exec_cmd(self, *a, **kw):
        counter["n"] += 1
        return await orig_exec(self, *a, **kw)

    async def pipe_exec(self, *a, **kw):
        counter["n"] += 1
        return await orig_pipe(self, *a, **kw)

    monkeypatch.setattr(Redis, "execute_command", exec_cmd)
    monkeypatch.setattr(Pipeline, "execute", pipe_exec)
    return counter


def test_round_trips_per_request(infra_settings, round_trips):
    """Default config (exact-only cache, Redis health, rate limiting): miss 10 -> 6 -> 7 (Phase 11.1), hit 5 -> 3."""
    with TestClient(create_app(infra_settings())) as c:
        c.post("/api/v1/chat", json={"query": "warm up connection pool"})
        round_trips["n"] = 0
        miss = c.post("/api/v1/chat", json={"query": "Explain Python variables"}).json()
        miss_rtt, round_trips["n"] = round_trips["n"], 0
        hit = c.post("/api/v1/chat", json={"query": "Explain Python variables"}).json()
        hit_rtt = round_trips["n"]
    assert miss["cache"]["status"] == "miss" and hit["cache"]["status"] == "hit_exact"
    # Phase 11.1 (H1): a miss makes ONE extra round trip - the atomic global cache admission check
    # (cache:{g}:idx, SemanticCache.store) that keeps cache memory below CACHE_MAX_ENTRIES across all
    # scopes and instances. It runs in its own slot, before the per-scope store transaction, so it
    # cannot share that round trip. Hits never store, so they stay at 3.
    assert miss_rtt <= 7 and hit_rtt <= 3, (miss_rtt, hit_rtt)


def test_store_releases_lock_and_waiters_still_get_the_entry(redis_prefix):
    async def go(cache):
        holder = await cache.lookup(SCOPE, "Explain Python variables")
        assert holder.lock_token
        lock_key = cache._lock(SCOPE, holder.query_hash)
        assert await cache.redis.client.exists(lock_key)
        assert await cache.store(holder, "Explain Python variables", {"answer": "a"})
        assert holder.lock_token is None                       # released inside the store transaction
        assert not await cache.redis.client.exists(lock_key)  # ...and really gone
        await cache.release(holder)                            # no-op, must not raise
        waiter = await cache.lookup(SCOPE, "Explain Python variables")
        return waiter

    m = redis_manager(redis_prefix)

    async def run():
        await m.start()
        try:
            return await go(SemanticCache(m, __import__("app.embeddings.local", fromlist=["x"]).LocalHashingEmbedder(256)))
        finally:
            await m.close()

    assert asyncio.run(run()).status == "hit_exact"


def test_lock_is_not_released_if_someone_else_owns_it(redis_prefix):
    """Release stays compare-and-delete inside the transaction (no stealing another holder's lock)."""
    from app.embeddings.local import LocalHashingEmbedder

    async def run():
        m = redis_manager(redis_prefix)
        await m.start()
        try:
            cache = SemanticCache(m, LocalHashingEmbedder(256))
            holder = await cache.lookup(SCOPE, "q")
            lock_key = cache._lock(SCOPE, holder.query_hash)
            await m.client.set(lock_key, "someone-else", px=30_000)  # lock expired and was re-taken
            await cache.store(holder, "q", {"answer": "a"})
            return await m.client.get(lock_key)
        finally:
            await m.close()

    assert asyncio.run(run()) == b"someone-else"


def test_counters_exact_under_concurrency(redis_prefix):
    from app.embeddings.local import LocalHashingEmbedder

    async def run():
        m = redis_manager(redis_prefix)
        await m.start()
        try:
            cache = SemanticCache(m, LocalHashingEmbedder(256), semantic_enabled=False)
            first = await cache.lookup(SCOPE, "hot query")
            await cache.store(first, "hot query", {"answer": "a"})
            await asyncio.gather(*[cache.lookup(SCOPE, "hot query") for _ in range(40)])
            misses = await asyncio.gather(*[cache.lookup(SCOPE, f"cold {i}") for i in range(25)])
            for lk in misses:
                await cache.release(lk)
            return await cache.stats()
        finally:
            await m.close()

    s = asyncio.run(run())
    assert (s.hits_exact, s.misses, s.stores) == (40, 26, 1)


def test_legacy_global_counters_are_still_reported_and_clear_keeps_stats(redis_prefix):
    from app.embeddings.local import LocalHashingEmbedder

    async def run():
        m = redis_manager(redis_prefix)
        await m.start()
        try:
            cache = SemanticCache(m, LocalHashingEmbedder(256))
            await m.client.hset(cache.k("cache", "stats"), mapping={"hits_exact": 7, "misses": 3})  # pre-6B data
            lk = await cache.lookup(SCOPE, "q1")
            await cache.store(lk, "q1", {"answer": "a"})
            await cache.lookup(SCOPE, "q1")
            before = await cache.stats()
            await cache.clear()
            after_clear = await cache.stats()
            await cache.clear(reset_stats=True)
            after_reset = await cache.stats()
            return before, after_clear, after_reset
        finally:
            await m.close()

    before, after_clear, after_reset = asyncio.run(run())
    assert (before.hits_exact, before.misses, before.total_entries) == (8, 4, 1)
    assert (after_clear.hits_exact, after_clear.misses, after_clear.total_entries) == (8, 4, 0)
    assert (after_reset.hits_exact, after_reset.misses) == (0, 0)


def test_record_success_returns_post_update_state(redis_prefix):
    from app.routing.health_store import LocalHealthStore, RedisHealthStore

    async def run():
        m = redis_manager(redis_prefix)
        await m.start()
        try:
            store = RedisHealthStore(m, failure_threshold=1, cooldown_seconds=60)
            await store.record_failure("x", "timeout")
            redis_state = await store.record_success("x")
            local = LocalHealthStore()
            await local.record_failure("y", "timeout")
            return redis_state, await local.record_success("y"), (await store.view(["x"])).state("x")
        finally:
            await m.close()

    redis_state, local_state, viewed = asyncio.run(run())
    assert redis_state == local_state == viewed == HealthState.HEALTHY


def test_provider_status_still_reported_on_success_and_fallback(infra_settings):
    s = infra_settings(cache_enabled=False, rate_limit_enabled=False, mock_failures={"mock-fast": "unavailable"},
                       provider_max_retries=0)
    with TestClient(create_app(s)) as c:
        body = c.post("/api/v1/chat", json={"query": "What is the capital of France?"}).json()
    assert body["fallback"]["used"] is True and body["provider_status"] == "healthy"


def test_waiter_survives_entry_write_and_release_between_its_get_and_exists(redis_prefix):
    """Deterministic regression for the race found in step 6B: the holder's store
    transaction (write entry + release lock) lands between the waiter's GET (miss)
    and its EXISTS(lock) check. The waiter must re-read and report a hit, not recompute."""
    from app.embeddings.local import LocalHashingEmbedder

    async def run():
        m = redis_manager(redis_prefix)
        await m.start()
        try:
            cache = SemanticCache(m, LocalHashingEmbedder(256), semantic_enabled=False,
                                  lock_wait_seconds=2, poll_interval_seconds=0.01)
            holder = await cache.lookup(SCOPE, "Explain Python variables")
            assert holder.lock_token
            real_exists = m.client.exists
            injected = {"done": False}

            async def exists_after_holder_stores(*keys):
                if not injected["done"]:
                    injected["done"] = True
                    await cache.store(holder, "Explain Python variables", {"answer": "from holder"})
                return await real_exists(*keys)

            m.client.exists = exists_after_holder_stores
            try:
                waiter = await cache.lookup(SCOPE, "Explain Python variables")
            finally:
                m.client.exists = real_exists
            return injected["done"], waiter
        finally:
            await m.close()

    injected, waiter = asyncio.run(run())
    assert injected  # the interleaving really happened
    assert waiter.status == "hit_exact" and waiter.payload["answer"] == "from holder"
    assert waiter.lock_token is None


def test_stampede_guard_under_repeated_concurrency(redis_prefix):
    """30 rounds x 6 concurrent identical lookups: exactly one computes each round."""
    from app.embeddings.local import LocalHashingEmbedder

    async def run():
        m = redis_manager(redis_prefix)
        await m.start()
        try:
            cache = SemanticCache(m, LocalHashingEmbedder(256), semantic_enabled=False, lock_wait_seconds=5)
            per_round = []
            for r in range(30):
                computed = 0

                async def request():
                    nonlocal computed
                    lk = await cache.lookup(SCOPE, f"round {r} hot question")
                    if not lk.hit:
                        computed += 1
                        await asyncio.sleep(0.02)
                        await cache.store(lk, f"round {r} hot question", {"answer": "a"})
                    await cache.release(lk)

                await asyncio.gather(*[request() for _ in range(6)])
                per_round.append(computed)
            return per_round
        finally:
            await m.close()

    assert asyncio.run(run()) == [1] * 30


def test_late_requester_does_not_win_a_freed_lock_and_recompute(redis_prefix):
    """Deterministic regression for the second race found in step 6B: request X's first
    GET misses, then the holder's store+release transaction completes, then X tries to
    lock. X must see the stored entry (double-checked inside the lock script)."""
    from app.embeddings.local import LocalHashingEmbedder

    async def run():
        m = redis_manager(redis_prefix)
        await m.start()
        try:
            cache = SemanticCache(m, LocalHashingEmbedder(256), semantic_enabled=False)
            holder = await cache.lookup(SCOPE, "Explain Python variables")
            real_get_counted = cache._get_counted
            calls = {"n": 0}

            async def get_then_holder_finishes(scope, qh, counter):
                result = await real_get_counted(scope, qh, counter)  # X's first GET: miss
                if calls["n"] == 0:
                    calls["n"] += 1
                    await cache.store(holder, "Explain Python variables", {"answer": "from holder"})
                return result

            cache._get_counted = get_then_holder_finishes
            late = await cache.lookup(SCOPE, "Explain Python variables")
            stats = await cache.stats()
            return late, stats
        finally:
            await m.close()

    late, stats = asyncio.run(run())
    assert late.status == "hit_exact" and late.payload["answer"] == "from holder"
    assert late.lock_token is None  # did NOT take the freed lock
    assert (stats.hits_exact, stats.misses) == (1, 1)  # counted once each, no double counting
