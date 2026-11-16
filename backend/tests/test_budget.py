"""Phase 5 step 8: spend safeguards (reserve -> call -> settle), with fake providers only."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.budget.guard import (
    COUNTER_TTL_MS, BudgetGuard, BudgetLimits, LocalBudgetStore, RedisBudgetStore, usd_to_micros,
)
from app.core.config import Settings
from app.main import create_app
from app.providers.litellm_env import import_litellm
from app.routing.errors import AllModelsFailedError, BudgetExceededError, BudgetUnavailableError
from app.routing.factory import build_chat_service
from app.routing.registry import ModelRegistry
from app.routing.service import ChatCommand
from app.routing.usage import upper_bound_input_tokens, worst_case_cost
from tests.conftest import redis_manager
from tests.provider_fakes import PROVIDER_MODELS, FakeCompletion, all_provider_registry, real_model, real_settings

Q = "What is the capital of France?"
DAY = 1_762_560_000.0  # fixed clock so day keys are deterministic


def run(coro):
    return asyncio.run(coro)


def chat(service, model=None, client="client-a", query=Q):
    return run(service.chat(ChatCommand(query=query, request_id="r", model_override=model, client_id=client)))


def guard(store=None, **limits) -> BudgetGuard:
    return BudgetGuard(store or LocalBudgetStore(), BudgetLimits(**limits), clock=lambda: DAY)


# =========================================================================== #
# Unit: arithmetic and worst-case estimate
# =========================================================================== #
def test_micros_round_up_so_tiny_costs_are_never_free():
    assert usd_to_micros(0) == 0
    assert usd_to_micros(0.0000001) == 1
    assert usd_to_micros(1.0) == 1_000_000
    assert usd_to_micros(0.1 + 0.2) == 300_000  # float noise does not add a micro


def test_input_upper_bound_is_at_least_utf8_bytes():
    assert upper_bound_input_tokens("abcd") == 4 + 32
    assert upper_bound_input_tokens("日本語") == 9 + 32  # multi-byte text is not underestimated


def test_worst_case_cost_uses_long_context_tier_when_the_prompt_is_large():
    m = real_model("anthropic", input_cost=0.10, output_cost=0.50, long_context_threshold_tokens=100_000,
                   long_context_input_cost_per_million=0.50, long_context_output_cost_per_million=2.50)
    assert worst_case_cost(m, 100_000, 1000) == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / 1e6)
    assert worst_case_cost(m, 100_001, 1000) == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)


def test_long_context_fields_must_be_complete_and_not_cheaper():
    with pytest.raises(ValueError):
        real_model("openai", long_context_threshold_tokens=10)
    with pytest.raises(ValueError):
        real_model("openai", input_cost=1.0, long_context_threshold_tokens=10,
                   long_context_input_cost_per_million=0.5, long_context_output_cost_per_million=5.0)


# =========================================================================== #
# Guard + local store
# =========================================================================== #
M = real_model("openai", input_cost=1_000_000.0, output_cost=0.0)  # 1 USD per input token: easy arithmetic


def test_global_daily_budget_is_enforced():
    g = guard(daily_usd=10)
    decisions = [run(g.reserve(M, "c1", 3, 0)) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[-1].reason == "global_daily_budget"


def test_per_client_budget_isolates_clients():
    g = guard(daily_usd=100, daily_usd_per_client=5)
    assert run(g.reserve(M, "c1", 5, 0)).allowed
    rejected = run(g.reserve(M, "c1", 1, 0))
    assert not rejected.allowed and rejected.reason == "client_daily_budget"
    assert run(g.reserve(M, "c2", 5, 0)).allowed


def test_per_client_token_limit():
    g = guard(daily_tokens_per_client=100)
    cheap = real_model("openai", input_cost=0.0001, output_cost=0.0001)
    assert run(g.reserve(cheap, "c1", 60, 40)).allowed
    d = run(g.reserve(cheap, "c1", 1, 0))
    assert not d.allowed and d.reason == "client_daily_tokens"


def test_per_request_limit_without_daily_tracking():
    g = guard(max_cost_per_request_usd=2)
    assert run(g.reserve(M, "c1", 2, 0)).allowed
    d = run(g.reserve(M, "c1", 3, 0))
    assert not d.allowed and d.reason == "per_request_limit" and d.estimated_cost_usd == pytest.approx(3)


def test_rejected_reservation_writes_nothing():
    store = LocalBudgetStore()
    g = guard(store, daily_usd=10, daily_usd_per_client=4)
    run(g.reserve(M, "c1", 4, 0))
    run(g.reserve(M, "c1", 1, 0))  # rejected by the client limit
    totals = run(store.totals(g.day(), g.client_key("c1")))
    assert totals["global_usd_micros"] == totals["client_usd_micros"] == 4_000_000


def test_settle_replaces_reservation_with_actual_amount():
    store = LocalBudgetStore()
    g = guard(store, daily_usd=10)
    d = run(g.reserve(M, "c1", 5, 0))
    run(g.settle(d.reservation, cost_usd=1.25, tokens=2))
    totals = run(store.totals(g.day(), g.client_key("c1")))
    assert totals == {"global_usd_micros": 1_250_000, "client_usd_micros": 1_250_000, "client_tokens": 2}
    run(g.settle(run(g.reserve(M, "c1", 2, 0)).reservation, cost_usd=None, tokens=None))  # unknown -> keep
    assert run(store.totals(g.day()))["global_usd_micros"] == 3_250_000


def test_budgets_reset_per_utc_day():
    store, now = LocalBudgetStore(), [DAY]
    g = BudgetGuard(store, BudgetLimits(daily_usd=1), clock=lambda: now[0])
    assert run(g.reserve(M, "c", 1, 0)).allowed and not run(g.reserve(M, "c", 1, 0)).allowed
    now[0] += 86_400
    assert run(g.reserve(M, "c", 1, 0)).allowed


class _Down:
    name = "down"

    async def reserve(self, *a, **k):
        raise ConnectionError("redis down")

    async def adjust(self, *a, **k):
        raise ConnectionError("redis down")


def test_store_outage_fails_closed_by_default_and_open_when_configured():
    closed = run(BudgetGuard(_Down(), BudgetLimits(daily_usd=1)).reserve(M, "c", 1, 0))
    opened = run(BudgetGuard(_Down(), BudgetLimits(daily_usd=1), fail_closed=False).reserve(M, "c", 1, 0))
    assert (closed.allowed, closed.reason, closed.degraded) == (False, "budget_unavailable", True)
    assert opened.allowed and opened.degraded


# =========================================================================== #
# Redis store (isolated DB 15 + unique prefix, cleaned up by the fixture)
# =========================================================================== #
@pytest.fixture
def redis_store(redis_prefix):
    manager = redis_manager(redis_prefix)

    async def make():
        await manager.start()
        return RedisBudgetStore(manager)

    yield manager, make


def test_redis_reservations_are_atomic_under_concurrency(redis_store):
    manager, make = redis_store

    async def scenario():
        store = await make()
        g = guard(store, daily_usd=10, daily_usd_per_client=1000)
        results = await asyncio.gather(*(g.reserve(M, f"c{i % 3}", 1, 0) for i in range(50)))
        totals = await store.totals(g.day())
        key = manager.keys("budget", "{" + g.day() + "}", "global", "usd")
        ttl = await manager.client.pttl(key)
        await manager.close()
        return results, totals, key, ttl

    results, totals, key, ttl = run(scenario())
    assert sum(r.allowed for r in results) == 10  # never more than the limit, whatever the interleaving
    assert totals["global_usd_micros"] == 10_000_000
    assert "{" in key and key.startswith(manager.keys.prefix)  # one hash tag per day (cluster-safe)
    assert 0 < ttl <= COUNTER_TTL_MS


def test_redis_settle_and_client_counters(redis_store):
    manager, make = redis_store

    async def scenario():
        store = await make()
        g = guard(store, daily_usd=10, daily_usd_per_client=3, daily_tokens_per_client=1000)
        d = await g.reserve(M, "alice", 3, 0)
        blocked = await g.reserve(M, "alice", 1, 0)
        await g.settle(d.reservation, cost_usd=0.5, tokens=7)
        after = await g.reserve(M, "alice", 2, 0)
        totals = await store.totals(g.day(), g.client_key("alice"))
        await manager.close()
        return blocked, after, totals

    blocked, after, totals = run(scenario())
    assert not blocked.allowed and after.allowed
    assert totals == {"global_usd_micros": 2_500_000, "client_usd_micros": 2_500_000, "client_tokens": 9}


# =========================================================================== #
# Integration: ChatService with fake real providers
# =========================================================================== #
def service(fake, registry=None, **settings):
    return build_chat_service(real_settings(**settings), registry=registry or all_provider_registry(),
                              completion_fn=fake)


def test_exhausted_budget_returns_429_and_never_calls_a_provider():
    fake = FakeCompletion()
    svc = service(fake, budget_daily_usd=0.000001)  # smaller than any worst-case estimate
    with pytest.raises(BudgetExceededError) as exc:
        chat(svc)
    assert fake.calls == []
    assert exc.value.status_code == 429 and exc.value.code == "budget_exceeded"
    assert exc.value.details["reasons"] == ["global_daily_budget"]


def test_per_request_limit_falls_back_to_a_cheaper_provider():
    reg = ModelRegistry([
        real_model("openai", input_cost=500.0, output_cost=500.0, fallbacks=["anthropic-model"]),
        real_model("anthropic", input_cost=0.1, output_cost=0.1, priority=20),
    ])
    fake = FakeCompletion()
    out = chat(service(fake, registry=reg, budget_max_cost_per_request_usd=0.05))
    assert out.model_used == "anthropic-model" and out.fallback.used
    assert [a.outcome for a in out.fallback.attempts] == ["skipped_budget", "success"]
    assert out.fallback.attempts[0].error_kind == "per_request_limit"
    assert [c["model"] for c in fake.calls] == [PROVIDER_MODELS["anthropic"]]


def test_successful_call_is_settled_to_the_reported_cost():
    fake = FakeCompletion({PROVIDER_MODELS["openai"]: [("usage", 1000, 500)]})
    svc = service(fake, budget_daily_usd=1.0)
    out = chat(svc, model="openai-model")
    totals = run(svc.budget.store.totals(svc.budget.day(), svc.budget.client_key("client-a")))
    assert out.cost.total_cost == pytest.approx((1000 * 1.0 + 500 * 2.0) / 1e6)
    assert totals["global_usd_micros"] == usd_to_micros(out.cost.total_cost)
    assert totals["client_tokens"] == 1500


def test_calls_rejected_before_work_are_released_but_timeouts_stay_reserved(tmp_path):
    lx = import_litellm().exceptions

    pm = PROVIDER_MODELS["openai"]
    fake = FakeCompletion({pm: [lx.AuthenticationError("bad key", "openai", pm)]})
    svc = service(fake, registry=ModelRegistry([real_model("openai")]))
    with pytest.raises(AllModelsFailedError):
        chat(svc, model="openai-model")
    assert run(svc.budget.store.totals(svc.budget.day()))["global_usd_micros"] == 0  # auth error: not billed

    slow = FakeCompletion({pm: [("sleep", 2.0)]})
    svc = service(slow, registry=ModelRegistry([real_model("openai", timeout=0.05)]), real_max_output_tokens=100)
    with pytest.raises(AllModelsFailedError):
        chat(svc, model="openai-model")
    expected = usd_to_micros(worst_case_cost(svc.policy.registry.get("openai-model"), upper_bound_input_tokens(Q), 100))
    assert run(svc.budget.store.totals(svc.budget.day()))["global_usd_micros"] == expected  # may have been billed


def test_retries_reserve_again_and_respect_the_budget():
    lx = import_litellm().exceptions

    pm = PROVIDER_MODELS["openai"]
    fake = FakeCompletion({pm: [lx.ServiceUnavailableError("down", "openai", pm)]})
    reg = ModelRegistry([real_model("openai", input_cost=10.0, output_cost=10.0)])
    one_attempt = worst_case_cost(reg.get("openai-model"), upper_bound_input_tokens(Q), 64)
    svc = service(fake, registry=reg, provider_max_retries=3, real_max_output_tokens=64,
                  budget_daily_usd=one_attempt * 2.5)  # room for exactly two kept reservations
    with pytest.raises(AllModelsFailedError) as exc:
        chat(svc, model="openai-model")
    assert len(fake.calls) == 2  # third attempt refused by the budget (5xx reservations are kept)
    assert [a["outcome"] for a in exc.value.details["attempts"]] == ["error", "error", "skipped_budget"]


def test_mock_mode_never_touches_the_budget():
    from app.routing.factory import build_chat_service as build

    s = Settings(_env_file=None, environment="test", budget_daily_usd=0.000001, mock_failures={},
                 redis_enabled=False, database_enabled=False, cache_enabled=False, rate_limit_enabled=False,
                 health_backend="memory")
    svc = build(s)
    assert chat(svc).mock_mode is True  # would be refused if the budget applied to mock calls
    assert run(svc.budget.store.totals(svc.budget.day()))["global_usd_micros"] == 0


def test_budget_store_outage_returns_503_via_the_api(monkeypatch, tmp_path):
    import json

    from tests.provider_fakes import FAKE_KEYS  # noqa: F401  (keys are fake)

    reg_file = tmp_path / "models.json"
    reg_file.write_text(json.dumps({"models": [json.loads(real_model("openai").model_dump_json())]}), "utf-8")
    fake = FakeCompletion()
    monkeypatch.setattr("app.providers.litellm_provider._load_acompletion", lambda: fake)
    # Redis enabled but pointing at a closed port -> budget store unreachable -> fail closed
    s = real_settings(model_registry_file=reg_file, redis_enabled=True, redis_port=1, redis_db=15, redis_host="127.0.0.1",
                      redis_connect_timeout_seconds=0.2, redis_socket_timeout_seconds=0.2, health_backend="memory")
    with TestClient(create_app(s), raise_server_exceptions=False) as c:
        r = c.post("/api/v1/chat", json={"query": Q})
        metrics = c.get("/metrics").text
    assert r.status_code == 503 and r.json()["error"]["code"] == "budget_unavailable"
    assert fake.calls == []
    assert 'llm_router_budget_rejections_total{reason="budget_unavailable"} 1.0' in metrics


def test_budget_exceeded_via_the_api_is_429_with_metrics(monkeypatch, tmp_path):
    import json

    reg_file = tmp_path / "models.json"
    reg_file.write_text(json.dumps({"models": [json.loads(real_model("openai").model_dump_json())]}), "utf-8")
    fake = FakeCompletion()
    monkeypatch.setattr("app.providers.litellm_provider._load_acompletion", lambda: fake)
    with TestClient(create_app(real_settings(model_registry_file=reg_file, budget_daily_usd=0.000001)),
                    raise_server_exceptions=False) as c:
        r = c.post("/api/v1/chat", json={"query": Q})
        metrics = c.get("/metrics").text
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_exceeded" and fake.calls == []
    assert 'llm_router_budget_rejections_total{reason="global_daily_budget"} 1.0' in metrics
    assert 'outcome="skipped_budget"' in metrics


def test_budget_unavailable_error_type():
    assert BudgetUnavailableError.status_code == 503 and BudgetUnavailableError.code == "budget_unavailable"


# =========================================================================== #
# Step 8 audit: additional safeguards
# =========================================================================== #
from app.budget.guard import NOT_BILLED  # noqa: E402
from app.providers.base import ProviderError, ProviderErrorKind  # noqa: E402


def test_fallback_chain_cannot_bypass_an_exhausted_budget():
    """Every model in the chain (all five providers) is checked; none is called."""
    fake = FakeCompletion()
    svc = service(fake, budget_daily_usd=0.000001, max_fallback_models=4)
    with pytest.raises(BudgetExceededError) as exc:
        chat(svc)
    assert fake.calls == []
    attempts = exc.value.details["attempts"]
    assert len(attempts) == 5 and {a["reason"] for a in attempts} == {"global_daily_budget"}


def test_fallback_is_charged_against_the_same_budget_as_the_failed_primary():
    """A kept (possibly billed) primary reservation leaves no room for the fallback."""
    pm = PROVIDER_MODELS["openai"]
    fake = FakeCompletion({pm: [ProviderError(ProviderErrorKind.UNAVAILABLE, "5xx", provider="openai")]})
    reg = ModelRegistry([real_model("openai", fallbacks=["anthropic-model"]), real_model("anthropic", priority=20)])
    per_attempt = worst_case_cost(reg.get("openai-model"), upper_bound_input_tokens(Q), 64)
    svc = service(fake, registry=reg, provider_max_retries=0, real_max_output_tokens=64,
                  budget_daily_usd=per_attempt * 1.5)
    with pytest.raises(AllModelsFailedError) as exc:
        chat(svc)
    assert [c["model"] for c in fake.calls] == [pm]  # the fallback was never called
    assert [a["outcome"] for a in exc.value.details["attempts"]] == ["error", "skipped_budget"]


# Reviewed expectation, written out literally (NOT derived from NOT_BILLED, so a change there fails here).
_RELEASED = {ProviderErrorKind.AUTH, ProviderErrorKind.BAD_REQUEST, ProviderErrorKind.NOT_FOUND,
             ProviderErrorKind.RATE_LIMIT, ProviderErrorKind.CONTEXT_WINDOW}
_KEPT = set(ProviderErrorKind) - _RELEASED


@pytest.mark.parametrize("kind", list(ProviderErrorKind))
def test_every_error_kind_has_an_explicit_billing_rule(kind):
    """Released only when the provider certainly did no billable work; everything else is kept."""
    pm = PROVIDER_MODELS["openai"]
    fake = FakeCompletion({pm: [ProviderError(kind, "simulated", provider="openai")]})
    reg = ModelRegistry([real_model("openai")])
    svc = service(fake, registry=reg, provider_max_retries=0, real_max_output_tokens=64)
    with pytest.raises(AllModelsFailedError):
        chat(svc, model="openai-model")
    spent = run(svc.budget.store.totals(svc.budget.day()))["global_usd_micros"]
    reserved = usd_to_micros(worst_case_cost(reg.get("openai-model"), upper_bound_input_tokens(Q), 64))
    if kind in _RELEASED:
        assert spent == 0
    else:
        assert spent == reserved, f"{kind} must keep the reservation (may have been billed)"


def test_billing_classification_is_the_reviewed_set():
    assert NOT_BILLED == _RELEASED
    assert {ProviderErrorKind.TIMEOUT, ProviderErrorKind.UNAVAILABLE, ProviderErrorKind.CONNECTION,
            ProviderErrorKind.UNKNOWN, ProviderErrorKind.INVALID_RESPONSE,
            ProviderErrorKind.CONTENT_POLICY} <= _KEPT


def test_long_prompt_is_reserved_at_the_long_context_rate():
    """The estimator applies the prompt-length tier: a limit between the two rates refuses the call."""
    long_q = "x" * 400  # upper bound 432 tokens > threshold 100
    m = real_model("anthropic", input_cost=1.0, output_cost=1.0, long_context_threshold_tokens=100,
                   long_context_input_cost_per_million=100.0, long_context_output_cost_per_million=100.0)
    flat = m.model_copy(update={"long_context_threshold_tokens": None, "long_context_input_cost_per_million": None,
                                "long_context_output_cost_per_million": None})
    base = worst_case_cost(flat, upper_bound_input_tokens(long_q), 64)
    long = worst_case_cost(m, upper_bound_input_tokens(long_q), 64)
    assert long == pytest.approx(base * 100)
    fake = FakeCompletion()
    svc = service(fake, registry=ModelRegistry([m]), real_max_output_tokens=64,
                  budget_max_cost_per_request_usd=(base + long) / 2)
    with pytest.raises(BudgetExceededError) as exc:
        chat(svc, model="anthropic-model", query=long_q)
    assert fake.calls == [] and exc.value.details["reasons"] == ["per_request_limit"]
    short_out = chat(svc, model="anthropic-model", query="hi")  # short prompt: base tier, allowed
    assert short_out.model_used == "anthropic-model"


def test_concurrent_requests_through_the_service_never_exceed_a_redis_budget(redis_store):
    """End to end: 10 overlapping requests, room for exactly 3 worst-case reservations."""
    manager, make = redis_store
    reg = ModelRegistry([real_model("openai")])
    per_attempt = worst_case_cost(reg.get("openai-model"), upper_bound_input_tokens(Q), 64)
    # No usage reported + no estimation -> every reservation is KEPT, so the result does not
    # depend on how far requests interleave (a settled request never frees room for a late one).
    fake = FakeCompletion({PROVIDER_MODELS["openai"]: [("sleep_no_usage", 0.3)]})

    async def scenario():
        store = await make()
        g = BudgetGuard(store, BudgetLimits(daily_usd=per_attempt * 3.5))
        svc = build_chat_service(real_settings(real_max_output_tokens=64, provider_max_retries=0,
                                               estimate_missing_usage=False), registry=reg,
                                 completion_fn=fake, budget=g)
        cmds = [ChatCommand(query=Q, request_id=f"r{i}", model_override="openai-model", client_id=f"c{i}")
                for i in range(10)]
        results = await asyncio.gather(*(svc.chat(c) for c in cmds), return_exceptions=True)
        await manager.close()
        return results

    results = run(scenario())
    ok = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, BudgetExceededError)]
    assert len(ok) == 3 and len(refused) == 7 and len(fake.calls) == 3


def test_redis_budget_rolls_over_at_utc_midnight_and_settles_on_the_reservation_day(redis_store):
    manager, make = redis_store
    now = [DAY + 86_400 - 1]  # one second before a UTC midnight boundary (DAY is a multiple of 86400)

    async def scenario():
        store = await make()
        g = BudgetGuard(store, BudgetLimits(daily_usd=1), clock=lambda: now[0])
        d1 = await g.reserve(M, "c", 1, 0)
        blocked = await g.reserve(M, "c", 1, 0)
        day1 = g.day()
        now[0] += 2  # next UTC day
        d2 = await g.reserve(M, "c", 1, 0)
        await g.settle(d1.reservation, cost_usd=0.25, tokens=1)  # settles DAY 1, not today
        totals = (await store.totals(day1), await store.totals(g.day()))
        await manager.close()
        return d1, blocked, d2, day1, totals

    d1, blocked, d2, day1, (t1, t2) = run(scenario())
    assert d1.allowed and not blocked.allowed and d2.allowed and d2.reservation.day != day1
    assert t1["global_usd_micros"] == 250_000 and t2["global_usd_micros"] == 1_000_000


def test_settle_failure_keeps_the_full_reservation():
    class FlakySettle(LocalBudgetStore):
        async def adjust(self, *a, **k):
            raise ConnectionError("redis down during settle")

    store = FlakySettle()
    g = guard(store, daily_usd=10)
    d = run(g.reserve(M, "c", 4, 0))
    run(g.settle(d.reservation, cost_usd=0.5, tokens=1))  # must not raise
    assert run(store.totals(g.day()))["global_usd_micros"] == 4_000_000  # errs on the safe side


def test_fail_open_allows_calls_when_configured_and_fail_closed_blocks_them():
    fake = FakeCompletion()
    reg = ModelRegistry([real_model("openai")])
    g = BudgetGuard(_Down(), BudgetLimits(daily_usd=1), fail_closed=False)
    svc = build_chat_service(real_settings(budget_fail_closed=False), registry=reg, completion_fn=fake, budget=g)
    assert chat(svc, model="openai-model").model_used == "openai-model" and len(fake.calls) == 1
    g_closed = BudgetGuard(_Down(), BudgetLimits(daily_usd=1), fail_closed=True)
    svc_closed = build_chat_service(real_settings(), registry=reg, completion_fn=fake, budget=g_closed)
    with pytest.raises(BudgetUnavailableError):
        chat(svc_closed, model="openai-model")
    assert len(fake.calls) == 1  # fail-closed: no additional provider call
