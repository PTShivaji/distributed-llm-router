"""Phase 5 step 9: end-to-end request lifecycle over HTTP, through the PRODUCTION wiring.

Unlike the unit/integration tests, nothing here injects a ChatService or a cache: every
app is built by `create_app(settings)` -> `build_container`, exactly as in deployment
(Redis budget store shared by chat and paid embeddings, recorder, metrics, tracing).
Only the two LiteLLM entry points are replaced with fakes:

    app.providers.litellm_provider._load_acompletion  -> FakeCompletion
    app.embeddings.litellm_embedder._load_aembedding  -> FakeEmbeddingFn
    app.providers.litellm_env.import_litellm          -> raises (any real LiteLLM use fails the test)

so no network call is possible and nothing is billed. Keys are fake strings; prices are
TEST VALUES. Redis: DB 15 + a unique test prefix; PostgreSQL: llm_router_test (truncated).
"""

import json
import logging
import re

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app.budget.embedding import embedding_cost
from app.budget.guard import BudgetGuard, BudgetLimits, LocalBudgetStore, usd_to_micros
from app.cache.semantic import SemanticCache
from app.main import create_app
from app.observability.metrics import Metrics
from app.routing.errors import BudgetExceededError
from app.routing.factory import build_chat_service
from app.routing.registry import ModelRegistry
from app.routing.service import ChatCommand
from app.routing.usage import TokenUsage, estimate_cost, upper_bound_input_tokens, worst_case_cost
from tests.conftest import redis_manager
from tests.provider_fakes import FAKE_KEYS, PROVIDER_MODELS, FakeCompletion, real_model
from tests.test_embedding_budget import EMB_MODEL, FakeEmbeddingFn, paid_embedder

CHAT = "/api/v1/chat"
PRICE = 1000.0  # TEST embedding price (USD / 1M input tokens)
OUT = 64  # REAL_MAX_OUTPUT_TOKENS used below
EMB_TOKENS = 7  # FakeEmbeddingFn reports 7 input tokens
SOURCE = "https://developers.openai.com/api/docs/pricing"
Q1 = "Explain python decorators thoroughly"
Q1_PARAPHRASE = "Describe python decorators thoroughly"
Q2 = "What is the capital of France?"
Q3 = "Summarise the plot of a short story"
Q_SHORT = "Name three primary colours"
ALICE_KEY = "alice-chat-key-for-e2e-tests-0001"
BOB_KEY = "bob-chat-key-for-e2e-tests-000002"
OBS_TOKEN = "observability-token-for-e2e-tests-01"
ADMIN_TOKEN = "admin-token-for-e2e-tests-000000001"
ALICE, BOB = {"Authorization": f"Bearer {ALICE_KEY}"}, {"Authorization": f"Bearer {BOB_KEY}"}
OBS, ADMIN = {"Authorization": f"Bearer {OBS_TOKEN}"}, {"Authorization": f"Bearer {ADMIN_TOKEN}"}

PRIMARY = real_model("openai", fallbacks=("anthropic-model",), priority=0)
SECONDARY = real_model("anthropic", priority=1)
DISABLED = real_model("mistral", enabled=False, pricing_is_placeholder=True)  # never gated, never usable


# --------------------------------------------------------------------------- #
# Arithmetic (all in integer micro-USD, as the budget store keeps it)
# --------------------------------------------------------------------------- #
def emb_worst(q: str) -> int:
    return usd_to_micros(embedding_cost(upper_bound_input_tokens(q), PRICE))


EMB_ACTUAL = usd_to_micros(embedding_cost(EMB_TOKENS, PRICE))


def chat_worst(q: str, model=PRIMARY) -> int:
    return usd_to_micros(worst_case_cost(model, upper_bound_input_tokens(q), OUT))


def chat_actual(model=PRIMARY) -> int:  # FakeCompletion reports 12 input + 30 output tokens
    return usd_to_micros(estimate_cost(TokenUsage(12, 30, 42, "provider_reported"), model).total_cost)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
class Probe:
    """Records the budget store's totals at the moment each paid call is made."""

    def __init__(self) -> None:
        self.guard: BudgetGuard | None = None
        self.seen: list[tuple[str, int]] = []

    async def snapshot(self, what: str) -> None:
        assert self.guard is not None
        try:
            totals = await self.guard.store.totals(self.guard.day())
        except Exception:  # budget store unreachable (Redis-outage tests): record, never fail the fake call
            self.seen.append((what, None))
            return
        self.seen.append((what, totals["global_usd_micros"]))


class ProbedEmbedding(FakeEmbeddingFn):
    def __init__(self, probe: Probe, *actions) -> None:
        super().__init__(*actions, reported_tokens=EMB_TOKENS)
        self.probe = probe

    async def __call__(self, **kwargs):
        await self.probe.snapshot("embedding")
        return await super().__call__(**kwargs)


class ProbedCompletion(FakeCompletion):
    def __init__(self, probe: Probe, script=None) -> None:
        super().__init__(script)
        self.probe = probe

    async def __call__(self, **kwargs):
        await self.probe.snapshot(kwargs["model"])
        return await super().__call__(**kwargs)


@pytest.fixture
def harness(infra_settings, tmp_path, monkeypatch):
    """Builds production-wired apps whose only fakes are the LiteLLM entry points."""

    def no_real_litellm():
        raise AssertionError("real LiteLLM was reached: this test must only use fakes")

    monkeypatch.setattr("app.providers.litellm_env.import_litellm", no_real_litellm)
    registry_file = tmp_path / "registry.json"
    registry_file.write_text(json.dumps({"models": [json.loads(m.model_dump_json())
                                                    for m in (PRIMARY, SECONDARY, DISABLED)]}), encoding="utf-8")

    def build(*, script=None, embedding=("ok",), real=True, **overrides):
        probe = Probe()
        emb, chat = ProbedEmbedding(probe, *embedding), ProbedCompletion(probe, script)
        monkeypatch.setattr("app.providers.litellm_provider._load_acompletion", lambda: chat)
        monkeypatch.setattr("app.embeddings.litellm_embedder._load_aembedding", lambda: emb)
        base = dict(
            log_level="INFO", chat_auth_mode="api_key", chat_api_keys={"alice": ALICE_KEY, "bob": BOB_KEY},
            observability_api_token=OBS_TOKEN, admin_api_token=ADMIN_TOKEN,
            tracing_enabled=True, tracing_exporter="memory", rate_limit_per_minute=100, rate_limit_per_hour=0,
            cache_semantic_enabled=True, budget_daily_usd=10.0, provider_max_retries=0,
        )
        if real:
            base |= dict(
                llm_mock_mode=False, allow_real_provider_calls=True, model_registry_file=registry_file,
                real_max_output_tokens=OUT, **{f"{p}_api_key": k for p, k in FAKE_KEYS.items()},
                embedding_provider="litellm", embedding_model=EMB_MODEL, embedding_input_cost_per_million=PRICE,
                embedding_pricing_source=SOURCE, embedding_pricing_is_placeholder=False,
            )
        base |= overrides
        client = TestClient(create_app(infra_settings(**base)), raise_server_exceptions=False)
        probe.guard = client.app.state.container.chat_service.budget
        return client, emb, chat, probe

    return build


def totals(c: TestClient, client_name: str | None = None) -> dict[str, int]:
    g = c.app.state.container.chat_service.budget
    client = g.client_key(f"chat-key:{client_name}") if client_name else None
    return c.portal.call(g.store.totals, g.day(), client)


def metric(text: str, name: str, **labels) -> float:
    total = 0.0
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
                total += sample.value
    return total


def flush(c: TestClient) -> None:
    c.portal.call(c.app.state.container.recorder.flush)


def db_rows(c: TestClient):
    from sqlalchemy import select

    from app.db.models import ProviderAttempt, RequestLog

    def query(session):
        logs = list(session.execute(select(RequestLog).order_by(RequestLog.created_at)).scalars())
        attempts = list(session.execute(select(ProviderAttempt).order_by(ProviderAttempt.id)).scalars())
        return ([{k: getattr(r, k) for k in ("request_id", "status", "error_code", "prompt_text", "response_text",
                                             "client_hash", "cache_status", "mock_mode", "attempt_count",
                                             "fallback_used", "model_used")} for r in logs],
                [(a.model_id, a.outcome) for a in attempts])

    return c.portal.call(c.app.state.container.database.run, query)


# =========================================================================== #
# 1. Startup, dependency health and separated credentials
# =========================================================================== #
def test_startup_dependency_health_and_every_protected_endpoint(harness, pg_clean):
    c, emb, chat, _ = harness(database_enabled=True)
    with c:
        assert c.get("/health").json()["status"] == "ok"
        deps = c.get("/api/v1/health/dependencies", headers=OBS).json()
        protected = {"/metrics": "get", "/api/v1/analytics/summary": "get", "/api/v1/cache/stats": "get",
                     "/api/v1/health/dependencies": "get"}
        anonymous = {p: getattr(c, m)(p).status_code for p, m in protected.items()}
        with_chat_key = {p: getattr(c, m)(p, headers=ALICE).status_code for p, m in protected.items()}
        with_admin = {p: getattr(c, m)(p, headers=ADMIN).status_code for p, m in protected.items()}
        with_obs = {p: getattr(c, m)(p, headers=OBS).status_code for p, m in protected.items()}
        chat_with_obs = c.post(CHAT, json={"query": Q2}, headers=OBS).status_code
        chat_with_admin = c.post(CHAT, json={"query": Q2}, headers=ADMIN).status_code
        delete_with_obs = c.delete("/api/v1/cache", headers=OBS).status_code
        delete_with_admin = c.delete("/api/v1/cache", headers=ADMIN).status_code

    assert deps["redis"] == {"enabled": True, "reachable": True}
    assert deps["database"] == {"enabled": True, "reachable": True}
    assert set(anonymous.values()) == set(with_chat_key.values()) == set(with_admin.values()) == {401}
    assert set(with_obs.values()) == {200}
    assert chat_with_obs == chat_with_admin == delete_with_obs == 401  # three separate credentials
    assert delete_with_admin == 200
    assert emb.calls == 0 and chat.calls == []  # nothing paid happened during any of this


# =========================================================================== #
# 2. Authentication, rate limiting and validation
# =========================================================================== #
def test_auth_failures_cost_no_quota_and_limits_are_per_key(harness):
    c, emb, chat, _ = harness(rate_limit_per_minute=2, cache_enabled=False)
    with c:
        unauthenticated = [c.post(CHAT, json={"query": Q2}).status_code for _ in range(3)]
        wrong = c.post(CHAT, json={"query": Q2}, headers={"Authorization": "Bearer not-a-valid-chat-key-0000"})
        first = c.post(CHAT, json={"query": Q2}, headers=ALICE)
        second = c.post(CHAT, json={"query": Q2}, headers=ALICE)
        third = c.post(CHAT, json={"query": Q2}, headers=ALICE)
        bob = c.post(CHAT, json={"query": Q2}, headers=BOB)

    assert unauthenticated == [401, 401, 401] and wrong.status_code == 401
    assert wrong.headers["WWW-Authenticate"].startswith("Bearer")
    # 4 rejected attempts consumed nothing: alice still gets her full quota of 2.
    assert first.status_code == 200 and first.headers["X-RateLimit-Remaining"] == "1"
    assert second.status_code == 200 and second.headers["X-RateLimit-Remaining"] == "0"
    assert third.status_code == 429 and third.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(third.headers["Retry-After"]) >= 1
    assert bob.status_code == 200  # per API key, not shared with alice
    assert len(chat.calls) == 3 and emb.calls == 0  # cache disabled -> no embeddings at all


def test_validation_unknown_and_disabled_models_never_reach_a_paid_call(harness):
    c, emb, chat, _ = harness()
    with c:
        blank = c.post(CHAT, json={"query": "   "}, headers=ALICE)
        extra = c.post(CHAT, json={"query": Q2, "api_key": FAKE_KEYS["openai"]}, headers=ALICE)
        too_long = c.post(CHAT, json={"query": "x" * 10_001}, headers=ALICE)
        unknown = c.post(CHAT, json={"query": Q2, "model": "no-such-model"}, headers=ALICE)
        disabled = c.post(CHAT, json={"query": Q2, "model": DISABLED.id}, headers=ALICE)

    assert blank.status_code == extra.status_code == too_long.status_code == 422
    assert FAKE_KEYS["openai"] not in extra.text and "x" * 100 not in too_long.text  # inputs are not echoed
    assert unknown.status_code == 400 and unknown.json()["error"]["code"] == "unknown_model"
    assert disabled.status_code == 400 and disabled.json()["error"]["code"] == "model_disabled"
    assert emb.calls == 0 and chat.calls == []


# =========================================================================== #
# 3. Full paid lifecycle: miss -> exact hit -> semantic hit (fake providers)
# =========================================================================== #
def test_full_paid_lifecycle_through_production_wiring(harness, pg_clean, caplog):
    caplog.set_level(logging.DEBUG)
    c, emb, chat, probe = harness(database_enabled=True, embedding=("const",))
    body = {"query": Q1, "model": PRIMARY.id}
    with c:
        miss = c.post(CHAT, json=body, headers={**ALICE, "X-Request-ID": "e2e-miss-1"})
        after_miss = totals(c, "alice")
        exact = c.post(CHAT, json=body, headers={**ALICE, "X-Request-ID": "e2e-exact-2"})
        after_exact = totals(c, "alice")
        semantic = c.post(CHAT, json={"query": Q1_PARAPHRASE, "model": PRIMARY.id},
                          headers={**ALICE, "X-Request-ID": "e2e-semantic-3"})
        after_semantic = totals(c, "alice")
        metrics_text = c.get("/metrics", headers=OBS).text
        flush(c)
        logs, attempts = db_rows(c)
        summary = c.get("/api/v1/analytics/summary", headers=OBS).json()
        spans = list(c.app.state.container.tracing.memory_exporter.get_finished_spans())
        same_guard = c.app.state.container.cache.embedder.guard is c.app.state.container.chat_service.budget

    # -- one guard for chat AND embeddings, and every paid call was reserved BEFORE it was made
    assert same_guard
    assert probe.seen == [("embedding", emb_worst(Q1)),
                          (PROVIDER_MODELS["openai"], EMB_ACTUAL + chat_worst(Q1)),
                          ("embedding", EMB_ACTUAL + chat_actual() + emb_worst(Q1_PARAPHRASE))]
    # -- settlement to provider-reported usage; shared global + per-client counters
    assert after_miss == {"global_usd_micros": EMB_ACTUAL + chat_actual(),
                          "client_usd_micros": EMB_ACTUAL + chat_actual(), "client_tokens": EMB_TOKENS + 42}
    assert after_exact == after_miss  # exact hit: no embedding, no chat, no spend
    assert after_semantic["global_usd_micros"] == 2 * EMB_ACTUAL + chat_actual()
    assert emb.calls == 2 and len(chat.calls) == 1

    # -- response metadata
    m = miss.json()
    assert miss.status_code == 200 and m["request_id"] == miss.headers["X-Request-ID"] == "e2e-miss-1"
    assert re.fullmatch(r"[0-9a-f]{32}", miss.headers["X-Trace-ID"])
    assert m["mock_mode"] is False and m["provider_adapter"] == "litellm" and m["model_used"] == PRIMARY.id
    assert m["classification"]["complexity"] in ("SIMPLE", "MEDIUM", "COMPLEX") and m["routing"]["override"]
    assert m["usage"] == {"input_tokens": 12, "output_tokens": 30, "total_tokens": 42, "source": "provider_reported"}
    assert m["cache"]["status"] == "miss" and m["cache"]["stored"] is True
    assert exact.json()["cache"]["status"] == "hit_exact" and exact.json()["usage"]["source"] == "cache_hit"
    assert semantic.json()["cache"]["status"] == "hit_semantic" and semantic.json()["answer"] == m["answer"]
    assert exact.json()["cache"]["source_request_id"] == "e2e-miss-1"

    # -- metrics
    assert metric(metrics_text, "llm_router_chat_requests_total", outcome="success") == 3
    for status in ("miss", "hit_exact", "hit_semantic"):
        assert metric(metrics_text, "llm_router_cache_lookups_total", status=status) == 1
    assert metric(metrics_text, "llm_router_embedding_budget_events_total", event="reserved") == 2
    assert metric(metrics_text, "llm_router_embedding_budget_events_total", event="settled") == 2
    assert metric(metrics_text, "llm_router_embedding_budget_events_total", event="refused") == 0

    # -- persistence + analytics (no raw text stored; client hashed)
    assert [r["request_id"] for r in logs] == ["e2e-miss-1", "e2e-exact-2", "e2e-semantic-3"]
    assert [r["cache_status"] for r in logs] == ["miss", "hit_exact", "hit_semantic"]
    assert all(r["prompt_text"] is None and r["response_text"] is None and r["mock_mode"] is False for r in logs)
    assert all(r["client_hash"] and "alice" not in r["client_hash"] for r in logs)
    assert attempts == [(PRIMARY.id, "success")]
    assert summary["total_requests"] == 3 and summary["cache_hits"] == 2 and summary["cache_misses"] == 1

    # -- tracing: the full lifecycle is in one trace per request
    miss_trace = int(miss.headers["X-Trace-ID"], 16)
    names = {s.name for s in spans if s.context.trace_id == miss_trace}
    assert {"chat", "classify", "route.select", "ratelimit.check", "cache.lookup", "cache.semantic_search",
            "embedding.generate", "provider.attempt", "cache.store"} <= names

    # -- nothing sensitive in logs, metrics, traces or responses
    span_text = " ".join(f"{s.name} {dict(s.attributes or {})} {[e.attributes for e in s.events]}" for s in spans)
    for secret in (*FAKE_KEYS.values(), ALICE_KEY, BOB_KEY, OBS_TOKEN, ADMIN_TOKEN):
        for where, text in (("logs", caplog.text), ("metrics", metrics_text), ("traces", span_text),
                            ("response", miss.text + exact.text + semantic.text)):
            assert secret not in text, f"secret leaked into {where}"
    for prompt in (Q1, Q1_PARAPHRASE):
        for where, text in (("logs", caplog.text), ("metrics", metrics_text), ("traces", span_text)):
            assert prompt not in text, f"prompt leaked into {where}"


# =========================================================================== #
# 4. Retry + fallback: a reservation before EVERY attempt, settlement per outcome
# =========================================================================== #
@pytest.fixture(scope="module")
def lx():
    """LiteLLM's exception classes (imported once; no network, no .env loading)."""
    from app.providers.litellm_env import import_litellm

    return import_litellm().exceptions


def test_retry_and_fallback_reserve_every_attempt_and_settle_by_outcome(harness, lx, pg_clean):
    pm = PROVIDER_MODELS["openai"]
    script = {pm: [lx.APIError(500, "upstream failure", "openai", pm)]}  # 5xx: may be billed -> kept
    c, emb, chat, probe = harness(script=script, provider_max_retries=1, database_enabled=True)
    with c:
        r = c.post(CHAT, json={"query": Q2}, headers={**ALICE, "X-Request-ID": "e2e-fallback"})
        spent = totals(c, "alice")
        metrics_text = c.get("/metrics", headers=OBS).text
        flush(c)
        logs, attempts = db_rows(c)

    body = r.json()
    assert r.status_code == 200 and body["fallback"]["used"] is True and body["model_used"] == SECONDARY.id
    assert [(a["model_id"], a["outcome"]) for a in body["fallback"]["attempts"]] == [
        (PRIMARY.id, "error"), (PRIMARY.id, "error"), (SECONDARY.id, "success")]
    assert len(chat.calls_for(pm)) == 2 and len(chat.calls_for(PROVIDER_MODELS["anthropic"])) == 1
    # Each attempt was reserved before its call; the two 5xx reservations are KEPT.
    worst_p, worst_s = chat_worst(Q2), chat_worst(Q2, SECONDARY)
    assert probe.seen == [("embedding", emb_worst(Q2)),
                          (pm, EMB_ACTUAL + worst_p),
                          (pm, EMB_ACTUAL + 2 * worst_p),
                          (PROVIDER_MODELS["anthropic"], EMB_ACTUAL + 2 * worst_p + worst_s)]
    assert spent["global_usd_micros"] == EMB_ACTUAL + 2 * worst_p + chat_actual(SECONDARY)
    assert body["cache"]["stored"] is False and emb.calls == 1  # fallback answers are not cached
    assert metric(metrics_text, "llm_router_retries_total", model=PRIMARY.id) == 1
    assert metric(metrics_text, "llm_router_fallbacks_total", from_model=PRIMARY.id, to_model=SECONDARY.id) == 1
    assert logs[0]["request_id"] == "e2e-fallback" and logs[0]["fallback_used"] is True
    assert attempts == [(PRIMARY.id, "error"), (PRIMARY.id, "error"), (SECONDARY.id, "success")]


def test_combined_per_request_limit_covers_embedding_retries_and_fallbacks(harness, lx):
    pm = PROVIDER_MODELS["openai"]
    script = {pm: [lx.APIError(500, "upstream failure", "openai", pm)]}
    worst_p = chat_worst(Q2)
    price = 1.0  # TEST value, low enough that the embedding worst case is below one chat attempt
    e_worst = usd_to_micros(embedding_cost(upper_bound_input_tokens(Q2), price))
    e_actual = usd_to_micros(embedding_cost(EMB_TOKENS, price))
    # Room for the embedding (settled to e_actual) and ONE chat attempt; the retry and fallback no longer fit.
    limit_micros = e_actual + worst_p + 1
    assert e_worst <= limit_micros < e_actual + 2 * worst_p
    c, emb, chat, _ = harness(script=script, provider_max_retries=1, embedding_input_cost_per_million=price,
                              budget_max_cost_per_request_usd=limit_micros / 1e6)
    with c:
        r = c.post(CHAT, json={"query": Q2}, headers=ALICE)
        metrics_text = c.get("/metrics", headers=OBS).text

    attempts = r.json()["error"]["details"]["attempts"]
    assert r.status_code == 503 and [(a["model"], a["outcome"], a["error_kind"]) for a in attempts] == [
        (PRIMARY.id, "error", "unavailable"),
        (PRIMARY.id, "skipped_budget", "per_request_limit"),
        (SECONDARY.id, "skipped_budget", "per_request_limit"),
    ]
    assert emb.calls == 1 and len(chat.calls) == 1  # no call beyond the request's limit
    assert metric(metrics_text, "llm_router_budget_rejections_total", reason="per_request_limit") == 2


# =========================================================================== #
# 5. Safe degradation when the embedding budget is refused
# =========================================================================== #
def test_refused_embedding_degrades_to_exact_cache_while_chat_continues(harness):
    # alice's per-client budget fits ONE embedding worst case (plus a little), not two.
    per_client = (emb_worst(Q2) + 100) / 1e6
    assert EMB_ACTUAL + chat_worst(Q2) <= per_client * 1e6 < EMB_ACTUAL + chat_actual() + emb_worst(Q3)
    assert emb_worst(Q_SHORT) <= per_client * 1e6  # bob's query fits his own (untouched) budget
    c, emb, chat, _ = harness(budget_daily_usd_per_client=per_client)
    with c:
        first = c.post(CHAT, json={"query": Q2, "model": PRIMARY.id}, headers=ALICE).json()
        degraded = c.post(CHAT, json={"query": Q3, "model": PRIMARY.id}, headers=ALICE).json()
        calls_after_degraded = (emb.calls, len(chat.calls))
        exact_again = c.post(CHAT, json={"query": Q3, "model": PRIMARY.id}, headers=ALICE).json()
        bob = c.post(CHAT, json={"query": Q_SHORT, "model": PRIMARY.id}, headers=BOB).json()
        metrics_text = c.get("/metrics", headers=OBS).text

    assert first["cache"]["status"] == "miss" and first["cache"]["stored"] is True
    # Embedding refused -> no paid embedding, semantic search skipped (a miss), chat still served
    # on its own reservation, and the answer stored as an exact-only entry.
    assert degraded["cache"]["status"] == "miss" and degraded["cache"]["stored"] is True
    assert calls_after_degraded == (1, 2)
    assert exact_again["cache"]["status"] == "hit_exact"
    assert bob["cache"]["status"] == "miss" and emb.calls == 2  # bob's own per-client budget
    assert len(chat.calls) == 3
    assert metric(metrics_text, "llm_router_embedding_budget_events_total", event="refused",
                  reason="client_daily_budget") == 1


def test_exhausted_budget_still_serves_exact_hits_and_refuses_everything_paid(harness, pg_clean):
    generous, _, _, _ = harness()
    with generous:
        stored = generous.post(CHAT, json={"query": Q2, "model": PRIMARY.id}, headers=ALICE).json()
    # Same Redis namespace (shared cache), but a global daily budget nothing fits into.
    c, emb, chat, _ = harness(budget_daily_usd=0.000001, database_enabled=True)
    with c:
        hit = c.post(CHAT, json={"query": Q2, "model": PRIMARY.id}, headers=ALICE)
        refused = c.post(CHAT, json={"query": Q3, "model": PRIMARY.id}, headers={**ALICE, "X-Request-ID": "e2e-429"})
        metrics_text = c.get("/metrics", headers=OBS).text
        flush(c)
        logs, attempts = db_rows(c)

    assert stored["cache"]["stored"] is True
    assert hit.status_code == 200 and hit.json()["cache"]["status"] == "hit_exact"
    assert refused.status_code == 429 and refused.json()["error"]["code"] == "budget_exceeded"
    assert emb.calls == 0 and chat.calls == []  # neither paid call was made
    # Step 9 regression: budget-refused chats are counted as failed chat requests.
    assert metric(metrics_text, "llm_router_chat_requests_total", outcome="error") == 1
    assert metric(metrics_text, "llm_router_chat_requests_total", outcome="success") == 1
    assert metric(metrics_text, "llm_router_embedding_budget_events_total", event="refused",
                  reason="global_daily_budget") == 1
    assert [(r["request_id"], r["status"], r["error_code"]) for r in logs][-1] == ("e2e-429", "error", "budget_exceeded")
    assert attempts == [(PRIMARY.id, "skipped_budget")]


# =========================================================================== #
# 6. Redis outage policy
# =========================================================================== #
DOWN = dict(redis_port=6399, redis_connect_timeout_seconds=0.2, redis_socket_timeout_seconds=0.2)


def test_redis_outage_fail_closed_refuses_paid_calls(harness):
    c, emb, chat, _ = harness(**DOWN)
    with c:
        deps = c.get("/api/v1/health/dependencies", headers=OBS).json()
        r = c.post(CHAT, json={"query": Q2}, headers=ALICE)
    assert deps["redis"]["reachable"] is False
    assert r.status_code == 503 and r.json()["error"]["code"] == "budget_unavailable"
    assert emb.calls == 0 and chat.calls == []


def test_redis_outage_fail_open_is_explicit_and_paid_embeddings_still_never_run(harness):
    c, emb, chat, _ = harness(budget_fail_closed=False, **DOWN)
    with c:
        r = c.post(CHAT, json={"query": Q2}, headers=ALICE)
    assert r.status_code == 200 and r.json()["cache"]["status"] == "error"  # documented fail-open for chat
    assert emb.calls == 0 and len(chat.calls) == 1


def test_redis_outage_with_rate_limit_fail_closed_rejects_before_anything_else(harness):
    c, emb, chat, _ = harness(rate_limit_fail_open=False, **DOWN)
    with c:
        r = c.post(CHAT, json={"query": Q2}, headers=ALICE)
    assert r.status_code == 503 and r.json()["error"]["code"] == "rate_limiter_unavailable"
    assert emb.calls == 0 and chat.calls == []


# =========================================================================== #
# 7. Mock mode end to end (the default): no budget, no LiteLLM, full pipeline
# =========================================================================== #
def test_mock_mode_lifecycle_never_touches_budget_or_litellm(harness, pg_clean):
    c, emb, chat, _ = harness(real=False, database_enabled=True)
    with c:
        miss = c.post(CHAT, json={"query": Q2}, headers=ALICE)
        hit = c.post(CHAT, json={"query": Q2}, headers=ALICE)
        spent = totals(c, "alice")
        metrics_text = c.get("/metrics", headers=OBS).text
        flush(c)
        logs, attempts = db_rows(c)

    assert miss.status_code == hit.status_code == 200
    assert miss.json()["mock_mode"] is True and miss.json()["provider_adapter"] == "mock"
    assert miss.json()["cache"]["status"] == "miss" and hit.json()["cache"]["status"] == "hit_exact"
    assert re.fullmatch(r"[0-9a-f]{32}", miss.headers["X-Trace-ID"])
    assert spent == {"global_usd_micros": 0, "client_usd_micros": 0, "client_tokens": 0}  # mock calls are free
    assert emb.calls == 0 and chat.calls == []  # local embedder; LiteLLM never loaded
    assert metric(metrics_text, "llm_router_chat_requests_total", outcome="success") == 2
    assert [r["mock_mode"] for r in logs] == [True, True] and len(attempts) == 1


# =========================================================================== #
# 8. Step 9 regression: a FAILED paid embedding is never called a second time
# =========================================================================== #
@pytest.mark.parametrize("failure", [TimeoutError("slow"), RuntimeError("upstream 500")])
def test_failed_lookup_embedding_is_not_retried_by_cache_store(redis_prefix, failure):
    import asyncio

    async def scenario():
        manager = redis_manager(redis_prefix)
        await manager.start()
        guard = BudgetGuard(LocalBudgetStore(), BudgetLimits(daily_usd=10))
        fn = FakeEmbeddingFn(failure, "ok")
        cache = SemanticCache(manager, paid_embedder(fn, guard), semantic_enabled=True)
        from tests.provider_fakes import real_settings

        svc = build_chat_service(real_settings(provider_max_retries=0, real_max_output_tokens=OUT),
                                 registry=ModelRegistry([PRIMARY, SECONDARY]), completion_fn=FakeCompletion(), cache=cache,
                                 budget=guard)
        first = await svc.chat(ChatCommand(query=Q1, request_id="r1", model_override=PRIMARY.id))
        calls_after_first = fn.calls
        again = await svc.chat(ChatCommand(query=Q1, request_id="r2", model_override=PRIMARY.id))
        await manager.close()
        return first, calls_after_first, again, fn.calls

    first, calls_after_first, again, calls = asyncio.run(scenario())
    assert calls_after_first == 1  # before step 9: 2 (store() re-embedded after the failure)
    assert first.cache.status == "miss" and first.cache.stored is True  # stored as an exact-only entry
    assert again.cache.status == "hit_exact" and calls == 1


def test_budget_refused_chat_is_counted_as_a_failed_chat_request():
    metrics = Metrics(model_ids=[PRIMARY.id], providers={"openai"}, mock_mode=False)
    from tests.provider_fakes import real_settings

    svc = build_chat_service(real_settings(), registry=ModelRegistry([PRIMARY, SECONDARY]), completion_fn=FakeCompletion(),
                             budget=BudgetGuard(LocalBudgetStore(), BudgetLimits(daily_usd=1e-6)), metrics=metrics)
    import asyncio

    with pytest.raises(BudgetExceededError):
        asyncio.run(svc.chat(ChatCommand(query=Q2, request_id="r1")))
    assert metrics.chat_requests.labels(complexity="SIMPLE", selected_model=PRIMARY.id, model_used="none",
                                        cache_status="disabled", outcome="error")._value.get() == 1
