"""ChatService + SemanticCache: provider is never called on a valid hit."""

import asyncio

from app.cache.semantic import SemanticCache
from app.embeddings.local import LocalHashingEmbedder
from app.providers.mock import MockProvider
from app.routing.service import ChatCommand
from tests.conftest import model, redis_manager

Q = "Explain Python variables"


def models():
    return [
        model("fast", "SIMPLE", fallbacks=("backup",)),
        model("backup", "SIMPLE", priority=500),
        model("advanced", "COMPLEX"),
    ]


async def run_with(prefix, make_service, scenario, *, failures=None, latency_ms=0, **cache_kw):
    m = redis_manager(prefix)
    await m.start()
    try:
        cache_kw.setdefault("allow_lexical", True)  # lexical semantic path is opt-in since step 6A
        cache = SemanticCache(m, LocalHashingEmbedder(256), **cache_kw)
        provider = MockProvider(failures or {}, latency_ms=latency_ms)
        svc = make_service(models(), provider=provider)
        svc.cache = cache
        return await scenario(svc, provider)
    finally:
        await m.close()


def chat(svc, query=Q, **kw):
    return svc.chat(ChatCommand(query=query, request_id=kw.pop("rid", "rid-1"), **kw))


def test_hit_does_not_call_provider(redis_prefix, make_service):
    async def scenario(svc, provider):
        first = await chat(svc)
        second = await chat(svc, rid="rid-2")
        third = await chat(svc, "explain python variables!", rid="rid-3")
        return first, second, third, provider.calls["fast"]

    first, second, third, calls = asyncio.run(run_with(redis_prefix, make_service, scenario))
    assert calls == 1
    assert first.cache.status == "miss" and first.cache.stored
    assert second.cache.status == "hit_exact" and third.cache.status == "hit_semantic"
    for hit in (second, third):
        assert hit.answer == first.answer
        assert hit.usage.source == "cache_hit" and hit.usage.total_tokens == 0 and hit.cost.total_cost == 0
        assert hit.provider_adapter == "cache" and hit.fallback.attempts == ()
        assert hit.cache.source_request_id == "rid-1"
        assert hit.cache.saved_tokens == first.usage.total_tokens
        assert hit.classification.complexity == "SIMPLE"  # fresh classification on every request
        assert hit.routing.selected_model == "fast"


def test_incompatible_override_and_settings_miss(redis_prefix, make_service):
    async def scenario(svc, provider):
        await chat(svc)
        override = await chat(svc, model_override="advanced")
        warm = await chat(svc, temperature=0.9)
        short = await chat(svc, max_output_tokens=10)
        same_model_override = await chat(svc, model_override="fast")
        return override, warm, short, same_model_override

    override, warm, short, same = asyncio.run(run_with(redis_prefix, make_service, scenario))
    assert override.cache.status == "miss" and override.model_used == "advanced"
    assert warm.cache.status == "miss"
    assert short.cache.status == "miss"
    assert same.cache.status == "hit_exact"  # explicit override of the SAME model is compatible


def test_use_cache_false_bypasses(redis_prefix, make_service):
    async def scenario(svc, provider):
        await chat(svc)
        out = await chat(svc, use_cache=False)
        return out, provider.calls["fast"]

    out, calls = asyncio.run(run_with(redis_prefix, make_service, scenario))
    assert out.cache.status == "bypass" and calls == 2


def test_fallback_answers_are_not_cached(redis_prefix, make_service):
    async def scenario(svc, provider):
        first = await chat(svc)
        second = await chat(svc, rid="rid-2")
        return first, second

    first, second = asyncio.run(run_with(redis_prefix, make_service, scenario, failures={"fast": "auth"}))
    assert first.fallback.used and first.model_used == "backup" and not first.cache.stored
    assert second.cache.status == "miss"


def test_concurrent_identical_requests_call_provider_once(redis_prefix, make_service):
    async def scenario(svc, provider):
        results = await asyncio.gather(*[chat(svc, rid=f"r{i}") for i in range(6)])
        return results, provider.calls["fast"]

    results, calls = asyncio.run(
        run_with(redis_prefix, make_service, scenario, latency_ms=150, semantic_enabled=False, lock_wait_seconds=5)
    )
    assert calls == 1
    assert sorted(r.cache.status for r in results) == ["hit_exact"] * 5 + ["miss"]
    assert len({r.answer for r in results}) == 1
