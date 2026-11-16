"""Phase 5 step 6A: semantic-cache correctness and safety.

Covers: exact duplicates, same-intent paraphrases, similar wording with different
intent, negation, changed numbers, entities, dates and technical parameters,
empty/malformed queries, safe-by-default gating and privacy of stored signatures.
"""

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.cache.equivalence import critical_signature, critical_tokens, has_word_content
from app.cache.semantic import SemanticCache, cache_scope
from app.core.config import Settings
from app.embeddings.base import cosine_similarity
from app.embeddings.local import LocalHashingEmbedder
from app.main import create_app
from tests.conftest import redis_manager

SCOPE = cache_scope(model_id="m", provider="mock", provider_model="mock/m", temperature=None,
                    max_output_tokens=1024, mock_mode=True, embedding_id="local-hashing-v1-256")
PAYLOAD = {"answer": "cached answer", "model_used": "m"}
CTX = ("in our production Kubernetes cluster running on AWS with three availability zones "
       "and an Nginx ingress controller in front of the API gateway")

SAME_INTENT = [
    ("Explain Python variables", "explain python variables!"),
    ("Explain Python variables", "  EXPLAIN   python Variables?  "),
    ("What is the capital of France?", "what is the capital of france"),
]

# Each pair differs in a detail that changes the correct answer, yet (measured) the
# lexical embedder scores it >= 0.92. The guard must reject every one of these.
MUST_MISS_GUARDED = {
    "negation_not": (f"Is it safe to restart the primary database {CTX}?",
                     f"Is it not safe to restart the primary database {CTX}?"),
    "negation_contraction": (f"Why does the deployment succeed {CTX}", f"Why doesn't the deployment succeed {CTX}"),
    "polarity_enable_disable": (f"Should I enable TLS for the Redis connection {CTX}?",
                                f"Should I disable TLS for the Redis connection {CTX}?"),
    "number_changed": (f"Configure the rate limiter to allow 100 requests per minute {CTX}",
                       f"Configure the rate limiter to allow 1000 requests per minute {CTX}"),
    "number_suffix": ("Design a distributed rate limiter that must handle 1M requests per second " + CTX,
                      "Design a distributed rate limiter that must handle 5M requests per second " + CTX),
    "decimal_vs_integer": (f"Set the timeout to 3.5 seconds {CTX}", f"Set the timeout to 35 seconds {CTX}"),
    "operator_changed": (f"Compute 1200 + 34 for the invoice {CTX}", f"Compute 1200 - 34 for the invoice {CTX}"),
    "month_changed": (f"Summarize the incident report from March 3 2024 {CTX}",
                      f"Summarize the incident report from April 3 2024 {CTX}"),
    "weekday_changed": (f"Schedule the maintenance window for Monday {CTX}",
                        f"Schedule the maintenance window for Friday {CTX}"),
    "iso_date_changed": (f"Show errors logged on 2024-01-02 {CTX}", f"Show errors logged on 2024-01-03 {CTX}"),
    "relative_date_changed": (f"Show the deploy plan for today {CTX}", f"Show the deploy plan for tomorrow {CTX}"),
    "version_changed": (f"Upgrade the service to Python 3.12 {CTX}", f"Upgrade the service to Python 3.13 {CTX}"),
    "port_changed": (f"Open port 8080 on host 10.0.0.5 {CTX}", f"Open port 8443 on host 10.0.0.5 {CTX}"),
    "ip_changed": (f"Open port 8080 on host 10.0.0.5 {CTX}", f"Open port 8080 on host 10.0.0.6 {CTX}"),
    "size_changed": (f"Allocate 4 GB of memory to the worker {CTX}", f"Allocate 16 GB of memory to the worker {CTX}"),
    "comparison_changed": (f"List the regions with more than ten nodes {CTX}",
                           f"List the regions with fewer than ten nodes {CTX}"),
}

# Measured lexical-guard blind spots: entity substitution and word order.
# These are the reason approximate hits are OFF by default for the lexical embedder.
GUARD_BLIND_SPOTS = {
    "entity_language": ("Write a Python function that parses a CSV file and returns the average of each column, "
                        "handling missing values.",
                        "Write a Java function that parses a CSV file and returns the average of each column, "
                        "handling missing values."),
    "entity_order": (f"Migrate the user table from PostgreSQL to MySQL {CTX}",
                     f"Migrate the user table from MySQL to PostgreSQL {CTX}"),
}


# =========================================================================== #
# Unit: equivalence guard
# =========================================================================== #
def test_exact_duplicates_share_signature():
    q = "Configure 3 replicas for the payment service"
    assert critical_signature(q) == critical_signature(q)


@pytest.mark.parametrize("a, b", SAME_INTENT)
def test_same_intent_variants_share_signature(a, b):
    assert critical_signature(a) == critical_signature(b)


@pytest.mark.parametrize("case", sorted(MUST_MISS_GUARDED))
def test_meaning_changes_change_the_signature(case):
    a, b = MUST_MISS_GUARDED[case]
    assert critical_signature(a) != critical_signature(b), case


def test_these_pairs_really_fool_the_lexical_embedder():
    """Evidence for the fix: without the guard, the similarity score alone would accept them."""
    e = LocalHashingEmbedder(256)
    fooled = [c for c, (a, b) in MUST_MISS_GUARDED.items()
              if cosine_similarity(e.embed_sync(a), e.embed_sync(b)) >= 0.92]
    assert len(fooled) >= 12, fooled


@pytest.mark.parametrize("case", sorted(GUARD_BLIND_SPOTS))
def test_documented_blind_spots_are_not_detected_by_the_guard(case):
    """Characterisation test: the lexical guard CANNOT see entity/order changes."""
    a, b = GUARD_BLIND_SPOTS[case]
    assert critical_signature(a) == critical_signature(b)


def test_critical_tokens_examples():
    assert critical_tokens("Compute 1200 + 34") == ["1200", "+", "34"]
    assert critical_tokens("It isn't safe on Monday") == ["isnt", "on", "monday"]
    assert critical_tokens("Use 16GB and 50% of 10.0.0.5") == ["16gb", "50%", "10.0.0.5"]
    assert critical_tokens("Explain Python variables") == []


@pytest.mark.parametrize("text, expected", [
    ("", False), ("   \n\t", False), ("???", False), ("!!! ...", False), ("🙂🙂🙂", False), ("\x00\x01", False),
    ("123 456", False), ("Explain variables", True), ("x", True), ("Qu'est-ce que c'est ?", True),
])
def test_has_word_content(text, expected):
    assert has_word_content(text) is expected


def test_signature_is_a_short_digest_not_the_tokens():
    sig = critical_signature("Transfer 99887766 to account 12345678")
    assert len(sig) == 16 and all(c in "0123456789abcdef" for c in sig)
    assert "99887766" not in sig


# =========================================================================== #
# Integration: SemanticCache against isolated Redis
# =========================================================================== #
async def _with_cache(prefix, fn, embedder=None, **kw):
    m = redis_manager(prefix)
    await m.start()
    try:
        return await fn(SemanticCache(m, embedder or LocalHashingEmbedder(256), **kw))
    finally:
        await m.close()


async def _store_then_lookup(cache, stored: str, probe: str):
    first = await cache.lookup(SCOPE, stored)
    assert first.status == "miss"
    assert await cache.store(first, stored, PAYLOAD)
    await cache.release(first)
    result = await cache.lookup(SCOPE, probe)
    await cache.release(result)
    return result


class MeaningAwareStub(LocalHashingEmbedder):
    """Stand-in for a real embedding model (no API calls): same vectors, flagged semantic."""

    is_semantic = True

    def __init__(self) -> None:
        super().__init__(256)
        self.id = "stub-semantic-256"


def test_lexical_semantic_is_gated_off_by_default(redis_prefix):
    async def go(cache):
        assert cache.semantic_requested and cache.semantic_gated and not cache.semantic_enabled
        r = await _store_then_lookup(cache, "Explain Python variables", "explain python variables!")
        assert r.status == "miss" and r.embedding is None  # no approximate matching attempted
        exact = await cache.lookup(SCOPE, "Explain Python variables")
        assert exact.status == "hit_exact"  # exact caching unaffected
        stats = await cache.stats()
        assert stats.semantic_enabled is False and any("DISABLED" in n for n in stats.notes)

    asyncio.run(_with_cache(redis_prefix, go))  # semantic_enabled=True requested, allow_lexical default False


@pytest.mark.parametrize("a, b", SAME_INTENT)
def test_opt_in_lexical_still_serves_same_intent_variants(redis_prefix, a, b):
    r = asyncio.run(_with_cache(redis_prefix, lambda c: _store_then_lookup(c, a, b), allow_lexical=True))
    assert r.status == "hit_semantic" and r.similarity >= 0.92


@pytest.mark.parametrize("case", sorted(MUST_MISS_GUARDED))
def test_opt_in_lexical_never_serves_meaning_changes(redis_prefix, case):
    a, b = MUST_MISS_GUARDED[case]
    r = asyncio.run(_with_cache(redis_prefix, lambda c: _store_then_lookup(c, a, b), allow_lexical=True))
    assert r.status == "miss", case
    assert r.guard_filtered == 1


@pytest.mark.parametrize("case", sorted(GUARD_BLIND_SPOTS))
def test_opt_in_lexical_blind_spots_documented(redis_prefix, case):
    """Characterisation: with the UNSAFE lexical opt-in these are still (wrongly) served."""
    a, b = GUARD_BLIND_SPOTS[case]
    r = asyncio.run(_with_cache(redis_prefix, lambda c: _store_then_lookup(c, a, b), allow_lexical=True))
    assert r.status == "hit_semantic"


def test_meaning_aware_embedder_enabled_without_lexical_flag_and_still_guarded(redis_prefix):
    async def go(cache):
        assert cache.semantic_enabled and not cache.semantic_gated
        same = await _store_then_lookup(cache, "Explain Python variables", "explain python variables?")
        a, b = MUST_MISS_GUARDED["number_changed"]
        changed = await _store_then_lookup(cache, a, b)
        return same, changed

    same, changed = asyncio.run(_with_cache(redis_prefix, go, embedder=MeaningAwareStub()))
    assert same.status == "hit_semantic"
    assert changed.status == "miss" and changed.guard_filtered >= 1


def test_entries_without_signature_never_match(redis_prefix):
    """Entries written before the guard existed (vector but no signature) are never semantic hits."""
    from app.embeddings.base import to_bytes

    async def go(cache):
        e = LocalHashingEmbedder(256)
        client = cache.redis.client
        await client.set(cache._entry(SCOPE, "legacy1"), json.dumps(PAYLOAD), ex=60)
        await client.zadd(cache._zset(SCOPE), {"legacy1": 9_999_999_999})
        await client.hset(cache._vectors(SCOPE), "legacy1", to_bytes(e.embed_sync("Explain Python variables")))
        r = await cache.lookup(SCOPE, "explain python variables")
        await cache.release(r)
        return r

    r = asyncio.run(_with_cache(redis_prefix, go, allow_lexical=True))
    assert r.status == "miss" and r.guard_filtered == 1


@pytest.mark.parametrize("query", ["", "   ", "???", "🙂🙂🙂", "\x00\x01\x02", "123 456", "x" * 10_000,
                                   "<script>alert(1)</script>", "Ünïcødé façade naïve"])
def test_malformed_queries_do_not_crash(redis_prefix, query):
    async def go(cache):
        first = await cache.lookup(SCOPE, query)
        assert first.status == "miss"
        stored = await cache.store(first, query, PAYLOAD)
        await cache.release(first)
        again = await cache.lookup(SCOPE, query)
        await cache.release(again)
        return stored, again

    stored, again = asyncio.run(_with_cache(redis_prefix, go, allow_lexical=True))
    assert stored and again.status == "hit_exact"  # exact caching still works for any input


def test_content_free_queries_never_semantic_match_each_other(redis_prefix):
    async def go(cache):
        r1 = await _store_then_lookup(cache, "???", "!!!")
        r2 = await _store_then_lookup(cache, "🙂🙂", "🙃🙃")
        return r1, r2

    r1, r2 = asyncio.run(_with_cache(redis_prefix, go, allow_lexical=True))
    assert r1.status == "miss" and r2.status == "miss"
    assert r1.embedding is None and r2.embedding is None  # semantic step skipped entirely


def test_exact_only_entries_are_indexed_counted_and_capped(redis_prefix):
    """Regression for a latent bug: exact-only entries were not indexed (count/TTL/cap)."""
    async def go(cache):
        for i in range(5):
            lk = await cache.lookup(SCOPE, f"unrelated question number {i}")
            await cache.store(lk, f"unrelated question number {i}", PAYLOAD)
            await cache.release(lk)
        stats = await cache.stats()
        return stats.total_entries, await cache.redis.client.hlen(cache._vectors(SCOPE))

    total, vectors = asyncio.run(_with_cache(redis_prefix, go, semantic_enabled=False, max_entries_per_scope=3))
    assert total == 3 and vectors == 0


def test_signatures_do_not_leak_prompt_content(redis_prefix):
    secret = "Transfer funds from account 99887766 to account 11223344 today"

    async def go(cache):
        await _store_then_lookup(cache, secret, "unrelated probe question")
        client = cache.redis.client
        blobs = []
        for key in [k async for k in client.scan_iter(match=redis_prefix + "*")]:
            blobs.append(key)
            kind = await client.type(key)
            if kind == b"string":
                blobs.append(await client.get(key) or b"")
            elif kind == b"hash":
                blobs.extend(v for v in (await client.hgetall(key)).values() if isinstance(v, bytes))
        return b"\n".join(blobs)

    dump = asyncio.run(_with_cache(redis_prefix, go, allow_lexical=True))
    for leaked in (b"99887766", b"11223344", b"Transfer", b"account"):
        assert leaked not in dump


# =========================================================================== #
# Settings / container / HTTP (API contract preserved)
# =========================================================================== #
def test_settings_default_semantic_off():
    # Declared defaults (the test suite's autouse fixture sets CACHE_ENABLED=false in the env).
    fields = Settings.model_fields
    assert fields["cache_semantic_enabled"].default is False
    assert fields["cache_semantic_allow_lexical"].default is False
    assert fields["cache_enabled"].default is True  # exact caching still on by default


def test_requesting_semantic_with_local_embedder_is_gated_and_logged(infra_settings, caplog):
    from app.infra.container import build_container

    with caplog.at_level(logging.WARNING):
        c = build_container(infra_settings(cache_semantic_enabled=True))
    assert c.cache.semantic_gated and not c.cache.semantic_enabled
    assert any(getattr(r, "event", None) == "cache.semantic_gated" for r in caplog.records)


def test_lexical_override_is_logged_as_unsafe(infra_settings, caplog):
    from app.infra.container import build_container

    with caplog.at_level(logging.WARNING):
        c = build_container(infra_settings(cache_semantic_enabled=True, cache_semantic_allow_lexical=True))
    assert c.cache.semantic_enabled
    assert any(getattr(r, "event", None) == "cache.semantic_lexical_unsafe" for r in caplog.records)


def test_http_default_config_serves_exact_hits_only(infra_settings):
    a, b = MUST_MISS_GUARDED["number_suffix"]
    with TestClient(create_app(infra_settings(rate_limit_enabled=False))) as c:
        first = c.post("/api/v1/chat", json={"query": "Explain Python variables"}).json()
        variant = c.post("/api/v1/chat", json={"query": "explain python variables!"}).json()
        repeat = c.post("/api/v1/chat", json={"query": "Explain Python variables"}).json()
        c.post("/api/v1/chat", json={"query": a})
        changed = c.post("/api/v1/chat", json={"query": b}).json()
        stats = c.get("/api/v1/cache/stats").json()
    assert first["cache"]["status"] == "miss"
    assert variant["cache"]["status"] == "miss"            # no approximate hit by default
    assert repeat["cache"]["status"] == "hit_exact"         # exact caching works
    assert changed["cache"]["status"] == "miss"
    assert stats["semantic_enabled"] is False and stats["hits_semantic"] == 0
    assert any("DISABLED" in n for n in stats["notes"])
    # Contract unchanged: same response fields and status vocabulary.
    assert set(first["cache"]) == {"status", "similarity", "threshold", "embedding_provider", "age_seconds",
                                   "source_request_id", "saved_tokens", "saved_cost", "stored", "lookup_ms"}


def test_http_opt_in_guarded_semantic(infra_settings):
    a, b = MUST_MISS_GUARDED["negation_not"]
    s = infra_settings(rate_limit_enabled=False, cache_semantic_enabled=True, cache_semantic_allow_lexical=True)
    with TestClient(create_app(s)) as c:
        c.post("/api/v1/chat", json={"query": "Explain Python variables"})
        variant = c.post("/api/v1/chat", json={"query": "explain python variables!"}).json()
        c.post("/api/v1/chat", json={"query": a})
        negated = c.post("/api/v1/chat", json={"query": b}).json()
    assert variant["cache"]["status"] == "hit_semantic"
    assert negated["cache"]["status"] == "miss"


@pytest.mark.parametrize("payload", [{"query": ""}, {"query": "   "}, {"query": None}, {}])
def test_empty_queries_rejected_before_the_cache(infra_settings, payload):
    with TestClient(create_app(infra_settings(rate_limit_enabled=False))) as c:
        r = c.post("/api/v1/chat", json=payload)
        stats = c.get("/api/v1/cache/stats").json()
    assert r.status_code == 422 and stats["misses"] == 0 and stats["hits"] == 0


@pytest.mark.parametrize("query", ["???", "🙂🙂🙂", "123 456"])
def test_content_free_queries_work_over_http(infra_settings, query):
    s = infra_settings(rate_limit_enabled=False, cache_semantic_enabled=True, cache_semantic_allow_lexical=True)
    with TestClient(create_app(s)) as c:
        first = c.post("/api/v1/chat", json={"query": query})
        second = c.post("/api/v1/chat", json={"query": query})
    assert first.status_code == second.status_code == 200
    assert second.json()["cache"]["status"] == "hit_exact"
