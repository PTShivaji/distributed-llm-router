"""Phase 5 step 8A: embedding spend protection.

Fake clients only: LiteLLMEmbedder gets an injected `embedding_fn`, so no
network call is ever made and LiteLLM is never used for a real request.
Prices below are TEST VALUES chosen for readable arithmetic, not real pricing.
Redis tests use DB 15 with a unique per-test prefix (cleaned up by the fixture).
"""

import asyncio
import logging

import pytest
from pydantic import SecretStr, ValidationError

from app.budget.embedding import BudgetedEmbedder, embedding_cost
from app.budget.guard import BudgetGuard, BudgetLimits, LocalBudgetStore, RedisBudgetStore, usd_to_micros
from app.cache.semantic import SemanticCache
from app.core.config import Settings
from app.embeddings.base import EmbeddingBudgetRefused, EmbeddingError
from app.embeddings.litellm_embedder import LiteLLMEmbedder
from app.embeddings.local import LocalHashingEmbedder
from app.infra.container import build_embedder
from app.observability.metrics import Metrics
from app.providers.base import ProviderErrorKind
from app.providers.catalog import ProviderConfigError, check_embedding_configuration
from app.providers.litellm_env import import_litellm
from app.routing.errors import BudgetExceededError
from app.routing.factory import build_chat_service
from app.routing.registry import ModelRegistry
from app.routing.service import ChatCommand
from app.routing.usage import upper_bound_input_tokens, worst_case_cost
from tests.conftest import redis_manager
from tests.provider_fakes import FAKE_KEYS, PROVIDER_MODELS, FakeCompletion, real_model, real_settings

Q = "What is the capital of France?"
EMB_MODEL = "openai/text-embedding-3-small"
PRICE = 1000.0  # TEST value (USD / 1M input tokens) -> 1 milli-USD per token
SOURCE = "https://developers.openai.com/api/docs/pricing"


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeEmbeddingFn:
    """Scripted stand-in for litellm.aembedding. Actions (last one repeats):
    "ok" (reports usage) | "no_usage" | BaseException instance | ("sleep", s) | "malformed"."""

    def __init__(self, *actions, reported_tokens: int = 7) -> None:
        self.actions = list(actions) or ["ok"]
        self.reported_tokens = reported_tokens
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        action = self.actions.pop(0) if len(self.actions) > 1 else self.actions[0]
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, tuple) and action[0] == "sleep":
            await asyncio.sleep(action[1])
            action = "no_usage"
        if action == "malformed":
            return {"data": []}
        if action == "const":  # every text maps to the same vector (similarity 1.0), with usage
            return {"data": [{"embedding": [1.0] + [0.0] * 63}], "usage": {"prompt_tokens": self.reported_tokens}}
        vector = LocalHashingEmbedder(64).embed_sync(kwargs["input"][0])
        response = {"data": [{"embedding": vector}]}
        if action == "ok":
            response["usage"] = {"prompt_tokens": self.reported_tokens, "total_tokens": self.reported_tokens}
        return response


def paid_embedder(fn, guard, *, timeout=5.0, metrics=None) -> BudgetedEmbedder:
    inner = LiteLLMEmbedder(EMB_MODEL, api_key=SecretStr(FAKE_KEYS["openai"]), timeout_seconds=timeout,
                            embedding_fn=fn)
    return BudgetedEmbedder(inner, guard, input_cost_per_million=PRICE, metrics=metrics)


def guard(store=None, **limits) -> BudgetGuard:
    return BudgetGuard(store or LocalBudgetStore(), BudgetLimits(**limits))


def worst(text: str) -> int:
    return usd_to_micros(embedding_cost(upper_bound_input_tokens(text), PRICE))


def gated_settings(**overrides) -> Settings:
    base = dict(_env_file=None, environment="test", llm_mock_mode=True, mock_failures={}, log_level="WARNING",
                redis_enabled=False, database_enabled=False, cache_enabled=False, rate_limit_enabled=False,
                health_backend="memory", embedding_provider="litellm", embedding_model=EMB_MODEL,
                allow_real_provider_calls=True, budget_daily_usd=1.0, openai_api_key=FAKE_KEYS["openai"],
                embedding_input_cost_per_million=0.02, embedding_pricing_source=SOURCE,
                embedding_pricing_is_placeholder=False)
    base.update(overrides)
    return Settings(**base)


def errors(s: Settings) -> set[str]:
    return {i.setting for i in check_embedding_configuration(s)}


# =========================================================================== #
# 1. Startup gate (mock and real modes)
# =========================================================================== #
def test_default_local_embedder_needs_no_opt_in_and_is_never_budgeted():
    s = Settings(_env_file=None)
    assert s.embedding_provider == "local" and s.llm_mock_mode is True
    assert check_embedding_configuration(s) == []
    embedder = build_embedder(s, budget=guard(daily_usd=1))
    assert isinstance(embedder, LocalHashingEmbedder)  # free; never wrapped


def test_fully_configured_paid_embedder_passes_and_is_always_wrapped():
    s = gated_settings()
    assert check_embedding_configuration(s) == []
    embedder = build_embedder(s, budget=guard(daily_usd=1))
    assert isinstance(embedder, BudgetedEmbedder) and isinstance(embedder.inner, LiteLLMEmbedder)
    assert embedder.input_cost_per_million == 0.02 and embedder.id == f"litellm:{EMB_MODEL}"


@pytest.mark.parametrize("mock_mode", [True, False])
def test_paid_embeddings_require_explicit_opt_in_in_mock_and_real_mode(mock_mode):
    s = gated_settings(llm_mock_mode=mock_mode, allow_real_provider_calls=False)
    assert "ALLOW_REAL_PROVIDER_CALLS" in errors(s)


def test_create_app_refuses_paid_embeddings_without_opt_in_even_in_mock_mode():
    from app.main import create_app

    with pytest.raises(ProviderConfigError) as exc:
        create_app(gated_settings(allow_real_provider_calls=False))
    assert "ALLOW_REAL_PROVIDER_CALLS" in str(exc.value) and FAKE_KEYS["openai"] not in str(exc.value)


def test_paid_embedder_without_a_budget_is_never_built():
    with pytest.raises(RuntimeError):
        build_embedder(gated_settings(), budget=None)


@pytest.mark.parametrize("overrides, setting", [
    ({"budget_daily_usd": 0}, "BUDGET_DAILY_USD"),
    ({"embedding_model": "text-embedding-3-small"}, "EMBEDDING_MODEL"),  # no provider prefix
    ({"embedding_model": "acme/embed-1"}, "EMBEDDING_MODEL"),  # unsupported provider
    ({"embedding_model": "ollama/nomic-embed-text"}, "EMBEDDING_MODEL"),  # local provider: not a paid path
    ({"embedding_model": "openai/"}, "EMBEDDING_MODEL"),
    ({"openai_api_key": None}, "OPENAI_API_KEY"),
    ({"embedding_model": "gemini/some-embedding-model"}, "GEMINI_API_KEY"),
    ({"embedding_pricing_is_placeholder": True}, "EMBEDDING_PRICING_IS_PLACEHOLDER"),
    ({"embedding_input_cost_per_million": 0.0}, "EMBEDDING_INPUT_COST_PER_MILLION"),
    ({"embedding_pricing_source": None}, "EMBEDDING_PRICING_SOURCE"),
    ({"embedding_pricing_source": ""}, "EMBEDDING_PRICING_SOURCE"),
    ({"embedding_pricing_source": "http://developers.openai.com/api/docs/pricing"}, "EMBEDDING_PRICING_SOURCE"),
    ({"embedding_pricing_source": "see the docs"}, "EMBEDDING_PRICING_SOURCE"),
    ({"embedding_pricing_source": "https://"}, "EMBEDDING_PRICING_SOURCE"),
])
def test_unsafe_embedding_configuration_is_refused(overrides, setting):
    s = gated_settings(**overrides)
    issues = check_embedding_configuration(s)
    assert setting in {i.setting for i in issues}
    assert all(i.severity == "error" for i in issues)
    assert not any(k in str(i) for i in issues for k in FAKE_KEYS.values())  # names settings, never values


@pytest.mark.parametrize("bad_price", [-0.01, float("inf"), float("nan"), "free"])
def test_invalid_embedding_prices_are_rejected_by_settings(bad_price):
    with pytest.raises(ValidationError) as exc:
        gated_settings(embedding_input_cost_per_million=bad_price)
    assert FAKE_KEYS["openai"] not in str(exc.value)  # validation errors never echo input


def test_budgeted_embedder_refuses_a_non_positive_price():
    with pytest.raises(ValueError):
        BudgetedEmbedder(LocalHashingEmbedder(8), guard(daily_usd=1), input_cost_per_million=0.0)


# =========================================================================== #
# 2. Reservation, refusal, settlement
# =========================================================================== #
def test_reservation_is_the_utf8_upper_bound_and_settles_to_reported_usage():
    fn, store = FakeEmbeddingFn("ok", reported_tokens=9), LocalBudgetStore()
    g = guard(store, daily_usd=10)
    reserved_seen = []
    original = store.reserve

    async def spy(day, client, cost, tokens, limits):
        reserved_seen.append((cost, tokens))
        return await original(day, client, cost, tokens, limits)

    store.reserve = spy
    text = "naïve café ☕"  # multi-byte: bytes, not characters, bound the tokens
    vector = run(paid_embedder(fn, g).embed(text))
    assert len(vector) == 64 and fn.calls == 1
    assert reserved_seen == [(worst(text), len(text.encode("utf-8")) + 32)]
    totals = run(store.totals(g.day(), g.client_key(None)))
    assert totals == {"global_usd_micros": usd_to_micros(embedding_cost(9, PRICE)), "client_usd_micros":
                      usd_to_micros(embedding_cost(9, PRICE)), "client_tokens": 9}


def test_exhausted_budget_refuses_without_calling_the_provider():
    fn = FakeEmbeddingFn()
    g = guard(daily_usd=embedding_cost(10, PRICE))  # far below one worst case
    with pytest.raises(EmbeddingBudgetRefused) as exc:
        run(paid_embedder(fn, g).embed(Q))
    assert fn.calls == 0 and exc.value.reason == "global_daily_budget"
    assert isinstance(exc.value, EmbeddingError)  # callers that handle embedding errors degrade safely


def test_missing_usage_keeps_the_full_reservation():
    store = LocalBudgetStore()
    g = guard(store, daily_usd=10)
    run(paid_embedder(FakeEmbeddingFn("no_usage"), g).embed(Q))
    assert run(store.totals(g.day()))["global_usd_micros"] == worst(Q)


@pytest.fixture(scope="module")
def lx():
    return import_litellm().exceptions  # exception classes only; no network


@pytest.mark.parametrize("make_exc, kept", [
    (lambda lx: lx.AuthenticationError("bad key", "openai", "m"), False),
    (lambda lx: lx.RateLimitError("slow down", "openai", "m"), False),
    (lambda lx: lx.BadRequestError("bad input", "openai", "m"), False),
    (lambda lx: lx.NotFoundError("no model", "openai", "m"), False),
    (lambda lx: lx.ServiceUnavailableError("5xx", "openai", "m"), True),
    (lambda lx: lx.InternalServerError("5xx", "openai", "m"), True),
    (lambda lx: lx.APIConnectionError("reset", "openai", "m"), True),
    (lambda lx: lx.Timeout("slow", "openai", "m"), True),
    (lambda lx: RuntimeError("unknown failure"), True),
])
def test_error_billing_rules_for_embeddings(lx, make_exc, kept):
    store = LocalBudgetStore()
    g = guard(store, daily_usd=10)
    with pytest.raises(EmbeddingError) as exc:
        run(paid_embedder(FakeEmbeddingFn(make_exc(lx)), g).embed(Q))
    assert not isinstance(exc.value, EmbeddingBudgetRefused)
    spent = run(store.totals(g.day()))["global_usd_micros"]
    assert spent == (worst(Q) if kept else 0)


def test_router_timeout_and_malformed_responses_keep_the_reservation():
    for fn, timeout in ((FakeEmbeddingFn(("sleep", 1.0)), 0.05), (FakeEmbeddingFn("malformed"), 5.0)):
        store = LocalBudgetStore()
        g = guard(store, daily_usd=10)
        with pytest.raises(EmbeddingError) as exc:
            run(paid_embedder(fn, g, timeout=timeout).embed(Q))
        assert exc.value.kind in (ProviderErrorKind.TIMEOUT, ProviderErrorKind.INVALID_RESPONSE)
        assert run(store.totals(g.day()))["global_usd_micros"] == worst(Q)


class _Down:
    name = "down"

    async def reserve(self, *a, **k):
        raise ConnectionError("redis down")

    async def adjust(self, *a, **k):
        raise ConnectionError("redis down")


@pytest.mark.parametrize("fail_closed", [True, False])
def test_budget_store_outage_never_allows_a_paid_embedding(fail_closed):
    """Even with BUDGET_FAIL_CLOSED=false, an embedding is never made without a reservation."""
    fn = FakeEmbeddingFn()
    g = BudgetGuard(_Down(), BudgetLimits(daily_usd=1), fail_closed=fail_closed)
    with pytest.raises(EmbeddingBudgetRefused) as exc:
        run(paid_embedder(fn, g).embed(Q))
    assert fn.calls == 0 and exc.value.reason == "budget_unavailable"


def test_embedding_budget_metrics_are_bounded():
    m = Metrics()
    g = guard(daily_usd=10)
    e = paid_embedder(FakeEmbeddingFn("ok"), g, metrics=m)
    run(e.embed(Q))
    e.guard = guard(daily_usd=embedding_cost(1, PRICE))
    with pytest.raises(EmbeddingBudgetRefused):
        run(e.embed(Q))
    m.embedding_budget("made-up-event", "made-up-reason")  # unknown values collapse to bounded labels

    def v(**labels):
        return m.registry.get_sample_value("llm_router_embedding_budget_events_total", labels) or 0

    assert v(event="reserved", reason="none") == 1 and v(event="settled", reason="none") == 1
    assert v(event="refused", reason="global_daily_budget") == 1
    assert v(event="other", reason="other") == 1
    assert m.registry.get_sample_value("llm_router_embedding_estimated_cost_usd_total") == pytest.approx(
        embedding_cost(7, PRICE))


def test_logs_never_contain_prompts_vectors_or_keys(caplog):
    caplog.set_level(logging.DEBUG)
    marker = "PRIVATE-PROMPT-MARKER zebra quartz"
    fn = FakeEmbeddingFn("ok")
    run(paid_embedder(fn, guard(daily_usd=10)).embed(marker))
    with pytest.raises(EmbeddingBudgetRefused):
        run(paid_embedder(fn, guard(daily_usd=embedding_cost(1, PRICE))).embed(marker))
    vector_text = str(LocalHashingEmbedder(64).embed_sync(marker)[:3])
    assert "PRIVATE-PROMPT-MARKER" not in caplog.text and FAKE_KEYS["openai"] not in caplog.text
    assert vector_text not in caplog.text and "embedding.budget_refused" in caplog.text


# =========================================================================== #
# 3. Shared budgets with chat, combined per-request limit (fake chat client)
# =========================================================================== #
# TEST prices. Chat is deliberately MORE expensive than one embedding, so an embedding's worst
# case always fits the limits below on its own (it is the combination that must not fit).
CHAT_MODEL = real_model("openai", input_cost=2000.0, output_cost=2000.0)
CHEAP_CHAT_MODEL = real_model("openai", input_cost=1.0, output_cost=1.0)


def chat_worst() -> int:
    return usd_to_micros(worst_case_cost(CHAT_MODEL, upper_bound_input_tokens(Q), 64))


def chat_actual() -> int:  # FakeCompletion reports 12 input + 30 output tokens
    return usd_to_micros((12 * 2000.0 + 30 * 2000.0) / 1e6)


async def _service_with_cache(prefix, g, emb_fn, completion=None, *, semantic=True, model=CHAT_MODEL,
                              **settings):
    manager = redis_manager(prefix)
    await manager.start()
    cache = SemanticCache(manager, paid_embedder(emb_fn, g), semantic_enabled=semantic)
    svc = build_chat_service(real_settings(real_max_output_tokens=64, provider_max_retries=0, **settings),
                             registry=ModelRegistry([model]), completion_fn=completion or FakeCompletion(),
                             cache=cache, budget=g)
    return manager, svc


def _cmd(query=Q, client="client-a", use_cache=True, rid="r1"):
    return ChatCommand(query=query, request_id=rid, model_override="openai-model", client_id=client,
                       use_cache=use_cache)


def test_chat_and_embedding_share_the_global_and_per_client_daily_counters(redis_prefix):
    fn, chat_fake = FakeEmbeddingFn("ok", reported_tokens=9), FakeCompletion()

    async def scenario():
        manager, svc = await _service_with_cache(redis_prefix, guard(), fn, chat_fake)
        store_g = BudgetGuard(RedisBudgetStore(manager), BudgetLimits(daily_usd=10, daily_usd_per_client=10))
        svc.budget = store_g
        svc.cache.embedder.guard = store_g
        out = await svc.chat(_cmd())
        totals = await store_g.store.totals(store_g.day(), store_g.client_key("client-a"))
        await manager.close()
        return out, totals

    out, totals = run(scenario())
    expected = usd_to_micros(embedding_cost(9, PRICE)) + chat_actual()
    assert out.model_used == "openai-model" and fn.calls == 1 and len(chat_fake.calls) == 1
    assert totals == {"global_usd_micros": expected, "client_usd_micros": expected, "client_tokens": 9 + 42}


def test_embedding_spend_can_exhaust_the_shared_daily_budget_for_chat(redis_prefix):
    """Daily limit fits the chat worst case alone, but not after this client's embedding spend."""
    fn, chat_fake = FakeEmbeddingFn("ok", reported_tokens=9), FakeCompletion()
    emb_actual = usd_to_micros(embedding_cost(9, PRICE))
    assert worst(Q) <= chat_worst() + emb_actual - 1  # the embedding alone fits

    async def scenario():
        manager, svc = await _service_with_cache(redis_prefix, guard(), fn, chat_fake)
        g = BudgetGuard(RedisBudgetStore(manager),
                        BudgetLimits(daily_usd=10, daily_usd_per_client=(chat_worst() + emb_actual - 1) / 1e6))
        svc.budget = svc.cache.embedder.guard = g
        try:
            await svc.chat(_cmd())
            return None
        except BudgetExceededError as exc:
            return exc
        finally:
            await manager.close()

    exc = run(scenario())
    assert exc is not None and exc.details["reasons"] == ["client_daily_budget"]
    assert fn.calls == 1 and chat_fake.calls == []


def test_per_request_limit_counts_embedding_and_chat_together(redis_prefix):
    """limit = chat worst case + half the embedding's real cost:
    chat alone fits, chat after this request's embedding does not."""
    emb_actual = usd_to_micros(embedding_cost(9, PRICE))
    limit = (chat_worst() + emb_actual // 2) / 1e6
    assert worst(Q) <= chat_worst() + emb_actual // 2  # the embedding alone fits

    async def scenario(use_cache):
        fn, chat_fake = FakeEmbeddingFn("ok", reported_tokens=9), FakeCompletion()
        manager, svc = await _service_with_cache(redis_prefix, guard(), fn, chat_fake)
        g = BudgetGuard(RedisBudgetStore(manager), BudgetLimits(daily_usd=10, max_cost_per_request_usd=limit))
        svc.budget = svc.cache.embedder.guard = g
        try:
            out = await svc.chat(_cmd(use_cache=use_cache, rid=f"r-{use_cache}"))
            return out, fn.calls, len(chat_fake.calls)
        except BudgetExceededError as exc:
            return exc, fn.calls, len(chat_fake.calls)
        finally:
            await manager.close()

    combined, emb_calls, chat_calls = run(scenario(True))
    assert isinstance(combined, BudgetExceededError) and combined.details["reasons"] == ["per_request_limit"]
    assert (emb_calls, chat_calls) == (1, 0)
    alone, emb_calls, chat_calls = run(scenario(False))  # no embedding -> the same limit admits the chat call
    assert alone.model_used == "openai-model" and (emb_calls, chat_calls) == (0, 1)


def test_refused_embedding_degrades_and_chat_continues_on_its_own_reservation(redis_prefix):
    """Per-request limit below the embedding worst case but above the chat worst case."""
    fn, chat_fake = FakeEmbeddingFn("ok"), FakeCompletion()
    long_q = Q + " " + "please answer in detail " * 20  # big embedding worst case
    cheap_chat_worst = usd_to_micros(worst_case_cost(CHEAP_CHAT_MODEL, upper_bound_input_tokens(long_q), 64))
    assert worst(long_q) > cheap_chat_worst

    async def scenario():
        manager, svc = await _service_with_cache(redis_prefix, guard(), fn, chat_fake, model=CHEAP_CHAT_MODEL)
        g = BudgetGuard(RedisBudgetStore(manager),
                        BudgetLimits(daily_usd=10, max_cost_per_request_usd=(cheap_chat_worst + 1) / 1e6))
        svc.budget = svc.cache.embedder.guard = g
        out = await svc.chat(_cmd(query=long_q))
        await manager.close()
        return out

    out = run(scenario())
    assert fn.calls == 0 and len(chat_fake.calls) == 1 and out.model_used == "openai-model"
    assert out.cache.status == "miss" and out.cache.stored is True


# =========================================================================== #
# 4. Degradation: exact caching keeps working when the embedding is refused
# =========================================================================== #
def _mock_service(manager, embedder):
    s = Settings(_env_file=None, environment="test", llm_mock_mode=True, mock_failures={}, log_level="WARNING",
                 redis_enabled=False, database_enabled=False, cache_enabled=False, rate_limit_enabled=False,
                 health_backend="memory")
    return build_chat_service(s, cache=SemanticCache(manager, embedder, semantic_enabled=True))


@pytest.mark.parametrize("store_kind", ["exhausted", "redis_down"])
def test_exact_cache_still_works_when_paid_embedding_is_refused(redis_prefix, store_kind):
    fn = FakeEmbeddingFn("ok")

    async def scenario():
        manager = redis_manager(redis_prefix)
        await manager.start()
        g = (BudgetGuard(_Down(), BudgetLimits(daily_usd=1)) if store_kind == "redis_down"
             else BudgetGuard(RedisBudgetStore(manager), BudgetLimits(daily_usd=embedding_cost(1, PRICE))))
        svc = _mock_service(manager, paid_embedder(fn, g))
        first = await svc.chat(ChatCommand(query=Q, request_id="a"))
        second = await svc.chat(ChatCommand(query=Q, request_id="b"))  # identical -> exact hit
        similar = await svc.chat(ChatCommand(query="What's the capital of France?", request_id="c"))
        await manager.close()
        return first, second, similar

    first, second, similar = run(scenario())
    assert fn.calls == 0  # the paid provider was never called
    assert first.cache.status == "miss" and first.cache.stored is True and first.mock_mode
    assert second.cache.status == "hit_exact" and second.cache.source_request_id == "a"
    assert similar.cache.status == "miss"  # approximate search skipped, not served from cache


def test_allowed_paid_embedding_still_enables_semantic_hits(redis_prefix):
    """Control: with budget available, approximate matching works as before (fake vectors)."""
    fn = FakeEmbeddingFn("const")

    async def scenario():
        manager = redis_manager(redis_prefix)
        await manager.start()
        g = BudgetGuard(RedisBudgetStore(manager), BudgetLimits(daily_usd=10))
        svc = _mock_service(manager, paid_embedder(fn, g))
        # same model -> same cache scope (routing could otherwise pick different tiers)
        await svc.chat(ChatCommand(query="Explain python decorators thoroughly", request_id="a",
                                   model_override="mock-fast"))
        out = await svc.chat(ChatCommand(query="Describe python decorators thoroughly", request_id="b",
                                         model_override="mock-fast"))
        totals = await g.store.totals(g.day())
        await manager.close()
        return out, totals

    out, totals = run(scenario())
    assert fn.calls == 2 and out.cache.status == "hit_semantic" and out.cache.source_request_id == "a"
    assert totals["global_usd_micros"] == 2 * usd_to_micros(embedding_cost(7, PRICE))  # both settled


def test_local_embedder_never_touches_the_budget(redis_prefix):
    class ExplodingStore(LocalBudgetStore):
        async def reserve(self, *a, **k):
            raise AssertionError("budget must not be used by the free local embedder")

        async def adjust(self, *a, **k):
            raise AssertionError("budget must not be used by the free local embedder")

    async def scenario():
        manager = redis_manager(redis_prefix)
        await manager.start()
        s = Settings(_env_file=None, environment="test", llm_mock_mode=True, mock_failures={},
                     redis_enabled=False, database_enabled=False, cache_enabled=False, rate_limit_enabled=False,
                     health_backend="memory")
        g = BudgetGuard(ExplodingStore(), BudgetLimits(daily_usd=1))
        embedder = build_embedder(s, budget=g)
        cache = SemanticCache(manager, embedder, semantic_enabled=True, allow_lexical=True)
        svc = build_chat_service(s, cache=cache, budget=g)
        outs = [await svc.chat(ChatCommand(query=q, request_id=str(i)))
                for i, q in enumerate([Q, Q, "Who wrote Hamlet?"])]
        await manager.close()
        return embedder, outs

    embedder, outs = run(scenario())
    assert isinstance(embedder, LocalHashingEmbedder)
    assert [o.cache.status for o in outs][:2] == ["miss", "hit_exact"]


# =========================================================================== #
# 5. Concurrency: atomic reservations in Redis
# =========================================================================== #
def test_concurrent_embedding_reservations_never_exceed_the_redis_budget(redis_prefix):
    fn = FakeEmbeddingFn(("sleep", 0.2))  # overlapping calls; no usage -> reservations are kept

    async def scenario():
        manager = redis_manager(redis_prefix)
        await manager.start()
        g = BudgetGuard(RedisBudgetStore(manager), BudgetLimits(daily_usd=worst(Q) * 3.5 / 1e6))
        e = paid_embedder(fn, g)
        results = await asyncio.gather(*(e.embed(Q) for _ in range(12)), return_exceptions=True)
        totals = await g.store.totals(g.day())
        await manager.close()
        return results, totals

    results, totals = run(scenario())
    allowed = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, EmbeddingBudgetRefused)]
    assert len(allowed) == 3 and len(refused) == 9 and fn.calls == 3
    assert totals["global_usd_micros"] == 3 * worst(Q)


def test_mixed_concurrent_chat_requests_with_embeddings_respect_one_budget(redis_prefix):
    """10 concurrent requests, each embedding + chat; the shared daily budget admits a bounded number."""
    # Neither embedding nor chat reports usage (and chat usage is not estimated), so every
    # reservation is KEPT: each served request costs its full worst case, whatever the interleaving.
    emb_fn = FakeEmbeddingFn(("sleep", 0.1))
    chat_fake = FakeCompletion({PROVIDER_MODELS["openai"]: [("sleep_no_usage", 0.1)]})
    per_request = worst(Q) + chat_worst()

    async def scenario():
        manager, svc = await _service_with_cache(redis_prefix, guard(), emb_fn, chat_fake,
                                                 estimate_missing_usage=False)
        g = BudgetGuard(RedisBudgetStore(manager), BudgetLimits(daily_usd=per_request * 2.5 / 1e6))
        svc.budget = svc.cache.embedder.guard = g
        cmds = [_cmd(query=f"{Q} variant {i}", client=f"c{i}", rid=f"r{i}") for i in range(10)]
        results = await asyncio.gather(*(svc.chat(c) for c in cmds), return_exceptions=True)
        totals = await g.store.totals(g.day())
        await manager.close()
        return results, totals, g

    results, totals, g = run(scenario())
    limit = usd_to_micros(g.limits.daily_usd)
    ok = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, BudgetExceededError)]
    assert len(ok) + len(refused) == 10  # every request either served or cleanly refused (429)
    # Room for at most 2.5 worst-case requests. 0 served is legitimate: if the 10 embedding
    # reservations land first (kept: no usage reported), no chat reservation fits any more.
    assert len(ok) <= 2 and len(refused) >= 8
    assert totals["global_usd_micros"] <= limit  # never overspent, whatever the interleaving
    assert len(chat_fake.calls) == len(ok)  # no chat call without a reservation
