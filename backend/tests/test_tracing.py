"""Phase 5 step 4: OpenTelemetry tracing - structure, propagation, safety, isolation."""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.trace import StatusCode

from app.core.config import Settings
from app.main import create_app
from app.observability.tracing import ALLOWED_ATTRIBUTES, MAX_ATTRIBUTE_LENGTH, Tracing, safe_attributes

CHAT = "/api/v1/chat"
Q = {"query": "What is the capital of France?"}
INCOMING_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
INCOMING_SPAN = "00f067aa0ba902b7"
TRACEPARENT = f"00-{INCOMING_TRACE}-{INCOMING_SPAN}-01"


def traced(**overrides) -> Settings:
    base = dict(_env_file=None, llm_mock_mode=True, mock_failures={}, redis_enabled=False, database_enabled=False,
                cache_enabled=False, rate_limit_enabled=False, health_backend="memory", log_level="INFO",
                provider_retry_backoff_seconds=0, tracing_enabled=True, tracing_exporter="memory")
    base.update(overrides)
    return Settings(**base)


def spans_of(c: TestClient):
    return list(c.app.state.container.tracing.memory_exporter.get_finished_spans())


def tree(spans) -> dict:
    """{span_name: parent_name} for spans with unique names; lists for repeated names."""
    by_id = {s.context.span_id: s for s in spans}
    out: dict[str, list[str | None]] = {}
    for s in spans:
        parent = by_id.get(s.parent.span_id).name if s.parent and s.parent.span_id in by_id else None
        out.setdefault(s.name, []).append(parent)
    return out


def hexid(n: int, width: int) -> str:
    return format(n, f"0{width}x")


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def test_tracing_disabled_by_default():
    s = Settings(_env_file=None)
    assert s.tracing_enabled is False and s.tracing_exporter == "none" and s.tracing_trust_incoming is False


def test_disabled_tracing_adds_nothing(test_settings, caplog):
    with caplog.at_level(logging.INFO), TestClient(create_app(test_settings)) as c:
        r = c.post(CHAT, json=Q)
        tracing = c.app.state.container.tracing
    assert not tracing.enabled and tracing.memory_exporter is None and tracing.provider is None
    assert "X-Trace-ID" not in r.headers
    assert all(getattr(rec, "trace_id", "-") == "-" for rec in caplog.records)


# --------------------------------------------------------------------------- #
# Trace structure
# --------------------------------------------------------------------------- #
def test_span_tree_for_successful_chat():
    with TestClient(create_app(traced())) as c:
        r = c.post(CHAT, json=Q)
        spans = spans_of(c)
    t = tree(spans)
    assert t["POST /api/v1/chat"] == [None]
    assert t["chat"] == ["POST /api/v1/chat"]
    for child in ("classify", "health.view", "route.select", "provider.chain"):
        assert t[child] == ["chat"], child
    assert t["provider.attempt"] == ["provider.chain"]
    assert t["health.record"] == ["provider.chain"]
    assert len({s.context.trace_id for s in spans}) == 1
    assert r.headers["X-Trace-ID"] == hexid(spans[0].context.trace_id, 32)

    http = next(s for s in spans if s.name == "POST /api/v1/chat")
    assert http.kind.name == "SERVER"
    assert http.attributes["http.route"] == "/api/v1/chat" and http.attributes["http.response.status_code"] == 200
    assert http.attributes["request_id"] == r.headers["X-Request-ID"]
    route = next(s for s in spans if s.name == "route.select")
    assert route.attributes["route.selected_model"] == "mock-fast" and route.attributes["route.requested_tier"] == "SIMPLE"
    classify = next(s for s in spans if s.name == "classify")
    assert classify.attributes["chat.complexity"] == "SIMPLE"


def test_retries_and_fallbacks_are_separate_attempt_spans():
    with TestClient(create_app(traced(mock_failures={"mock-fast": "unavailable"}, provider_max_retries=1))) as c:
        c.post(CHAT, json=Q)
        spans = spans_of(c)
    attempts = sorted((s for s in spans if s.name == "provider.attempt"), key=lambda s: s.attributes["llm.attempt"])
    assert [(a.attributes["llm.model"], a.attributes["llm.retry"], a.attributes["llm.is_fallback"]) for a in attempts] == [
        ("mock-fast", 0, False), ("mock-fast", 1, False), ("mock-balanced", 0, True),
    ]
    assert [a.status.status_code for a in attempts] == [StatusCode.ERROR, StatusCode.ERROR, StatusCode.UNSET]
    assert attempts[0].attributes["llm.error_kind"] == "unavailable" and attempts[0].status.description == "unavailable"
    chain = next(s for s in spans if s.name == "provider.chain")
    assert chain.attributes["chat.fallback_used"] is True and chain.attributes["chat.model_used"] == "mock-balanced"


def test_all_models_failed_marks_spans_as_errors():
    every = {m: "unavailable" for m in ["mock-fast", "mock-balanced", "mock-coder", "mock-advanced"]}
    with TestClient(create_app(traced(mock_failures=every, provider_max_retries=0))) as c:
        assert c.post(CHAT, json=Q).status_code == 503
        spans = {s.name: s for s in spans_of(c)}
    assert spans["provider.chain"].status.status_code == StatusCode.ERROR
    assert spans["chat"].status.description == "AllModelsFailedError"
    assert spans["POST /api/v1/chat"].attributes["error.category"] == "provider"
    assert spans["POST /api/v1/chat"].status.status_code == StatusCode.ERROR


def test_timeout_attempt_span():
    with TestClient(create_app(traced(mock_failures={"mock-fast": "timeout"}))) as c:
        c.app.state.container.chat_service.policy.registry._models["mock-fast"] = (
            c.app.state.container.chat_service.policy.registry.get("mock-fast").model_copy(update={"timeout_seconds": 0.05}))
        c.post(CHAT, json=Q)
        attempts = [s for s in spans_of(c) if s.name == "provider.attempt"]
    timeout = next(a for a in attempts if a.attributes["llm.model"] == "mock-fast")
    assert timeout.attributes["llm.error_kind"] == "timeout" and timeout.status.status_code == StatusCode.ERROR
    assert (timeout.end_time - timeout.start_time) / 1e9 >= 0.05


def test_unmatched_and_validation_requests_traced_with_template_names():
    with TestClient(create_app(traced())) as c:
        c.get("/scan/abc/123?x=1")
        c.post(CHAT, json={})
        names = [s.name for s in spans_of(c)]
    assert "GET unmatched" in names and "POST /api/v1/chat" in names
    assert not any("abc" in n or "123" in n for n in names)


# --------------------------------------------------------------------------- #
# Log correlation
# --------------------------------------------------------------------------- #
def test_logs_carry_trace_id_and_request_id(caplog):
    with caplog.at_level(logging.INFO), TestClient(create_app(traced())) as c:
        r = c.post(CHAT, json=Q, headers={"X-Request-ID": "trace-corr-1"})
    trace_id = r.headers["X-Trace-ID"]
    app_records = [rec for rec in caplog.records if rec.name.startswith("app.") and rec.request_id == "trace-corr-1"]
    assert {getattr(rec, "event", None) for rec in app_records} >= {"chat.request_received", "provider.success", "http.request"}
    assert {rec.trace_id for rec in app_records} == {trace_id}
    assert all(rec.trace_suffix == f" trace={trace_id}" for rec in app_records)


# --------------------------------------------------------------------------- #
# W3C traceparent handling
# --------------------------------------------------------------------------- #
def test_untrusted_traceparent_becomes_link_not_parent():
    with TestClient(create_app(traced())) as c:
        r = c.post(CHAT, json=Q, headers={"traceparent": TRACEPARENT})
        http = next(s for s in spans_of(c) if s.kind.name == "SERVER")
    assert r.headers["X-Trace-ID"] != INCOMING_TRACE
    assert http.parent is None
    assert len(http.links) == 1
    assert hexid(http.links[0].context.trace_id, 32) == INCOMING_TRACE
    assert http.links[0].attributes["trace.incoming_traceparent"] == "untrusted"


def test_trusted_traceparent_continues_the_trace():
    with TestClient(create_app(traced(tracing_trust_incoming=True))) as c:
        r = c.post(CHAT, json=Q, headers={"traceparent": TRACEPARENT})
        spans = spans_of(c)
    http = next(s for s in spans if s.kind.name == "SERVER")
    assert r.headers["X-Trace-ID"] == INCOMING_TRACE
    assert hexid(http.parent.span_id, 16) == INCOMING_SPAN and http.parent.is_remote
    assert {hexid(s.context.trace_id, 32) for s in spans} == {INCOMING_TRACE}


@pytest.mark.parametrize("bad", [
    "garbage",
    f"00-{'0' * 32}-{INCOMING_SPAN}-01",          # all-zero trace id
    f"00-{INCOMING_TRACE}-{'0' * 16}-01",         # all-zero span id
    f"ff-{INCOMING_TRACE}-{INCOMING_SPAN}-01",    # forbidden version
    f"00-{INCOMING_TRACE}-{INCOMING_SPAN}-01-extra-junk",
    "00-" + "a" * 5000,                           # oversized
])
def test_invalid_traceparent_is_ignored(bad):
    with TestClient(create_app(traced(tracing_trust_incoming=True))) as c:
        r = c.post(CHAT, json=Q, headers={"traceparent": bad})
        http = next(s for s in spans_of(c) if s.kind.name == "SERVER")
    assert r.status_code == 200
    assert http.parent is None and len(http.links) == 0
    assert r.headers["X-Trace-ID"] != INCOMING_TRACE


def test_baggage_is_never_propagated():
    with TestClient(create_app(traced(tracing_trust_incoming=True))) as c:
        c.post(CHAT, json=Q, headers={"traceparent": TRACEPARENT, "baggage": "user.email=leak%40example.com"})
        dump = json.dumps([json.loads(s.to_json()) for s in spans_of(c)])
    assert "leak" not in dump and "example.com" not in dump


# --------------------------------------------------------------------------- #
# Cache, embeddings, Redis (isolated namespace)
# --------------------------------------------------------------------------- #
def test_cache_miss_and_hit_spans(infra_settings):
    # semantic_search/embedding spans only exist when approximate matching is on (opt-in since step 6A)
    s = infra_settings(tracing_enabled=True, tracing_exporter="memory", rate_limit_enabled=True,
                       cache_semantic_enabled=True, cache_semantic_allow_lexical=True)
    with TestClient(create_app(s)) as c:
        c.post(CHAT, json=Q)
        miss = spans_of(c)
        c.app.state.container.tracing.memory_exporter.clear()
        c.post(CHAT, json=Q)
        hit = spans_of(c)
    t = tree(miss)
    assert t["cache.lookup"] == ["chat"]
    assert t["cache.exact_get"] == ["cache.lookup"]
    assert t["cache.semantic_search"] == ["cache.lookup"]
    assert "cache.semantic_search" in t["embedding.generate"]
    assert t["cache.lock"] == ["cache.lookup"]
    assert t["cache.store"] == ["chat"]
    assert t["ratelimit.check"] == ["POST /api/v1/chat"]
    lookup = next(x for x in miss if x.name == "cache.lookup")
    assert lookup.attributes["cache.status"] == "miss" and lookup.attributes["cache.lock_acquired"] is True
    embed = next(x for x in miss if x.name == "embedding.generate")
    assert embed.attributes["embedding.provider"] == "local-hashing-v1-256" and embed.attributes["embedding.dimensions"] == 256
    rl = next(x for x in miss if x.name == "ratelimit.check")
    assert rl.attributes["ratelimit.scope"] == "client" and rl.attributes["db.system"] == "redis"

    hit_names = [x.name for x in hit]
    assert "provider.chain" not in hit_names and "provider.attempt" not in hit_names  # no provider call on a hit
    assert next(x for x in hit if x.name == "cache.lookup").attributes["cache.status"] == "hit_exact"


# --------------------------------------------------------------------------- #
# PostgreSQL persistence: background spans linked to request traces (isolated DB)
# --------------------------------------------------------------------------- #
def test_persistence_spans_link_to_request_and_propagate_into_threads(infra_settings, pg_clean):
    s = infra_settings(tracing_enabled=True, tracing_exporter="memory", database_enabled=True, rate_limit_enabled=False)
    with TestClient(create_app(s)) as c:
        r = c.post(CHAT, json=Q)
        c.portal.call(c.app.state.container.recorder.flush)
        c.get("/api/v1/analytics/summary")
        spans = spans_of(c)
    request_trace = r.headers["X-Trace-ID"]
    persist = next(x for x in spans if x.name == "db.persist_batch")
    insert = next(x for x in spans if x.name == "db.insert_batch")
    assert persist.parent is None  # background worker: its own trace...
    assert {hexid(link.context.trace_id, 32) for link in persist.links} == {request_trace}  # ...linked to the request
    assert insert.parent.span_id == persist.context.span_id  # context crossed into the worker thread
    assert persist.attributes["db.rows"] == 1 and persist.attributes["db.outcome"] == "written"
    analytics = next(x for x in spans if x.name == "db.analytics_summary")
    assert analytics.attributes["db.system"] == "postgresql" and analytics.parent is not None


# --------------------------------------------------------------------------- #
# Safety: no sensitive data anywhere in spans
# --------------------------------------------------------------------------- #
def test_spans_contain_no_sensitive_data(infra_settings, pg_clean):
    secrets = {"openai_api_key": "sk-proj-TRACESECRETKEY0001", "admin_api_token": "trace-admin-token-0002",
               "postgres_password": "router_password"}
    prompt = "Confidential: the falcon lands at dawn. What is the capital of France?"
    s = infra_settings(tracing_enabled=True, tracing_exporter="memory", database_enabled=True,
                       mock_failures={"mock-fast": "auth"}, **{k: v for k, v in secrets.items() if k != "postgres_password"})
    with TestClient(create_app(s)) as c:
        r = c.post(CHAT, json={"query": prompt}, headers={
            "Authorization": "Bearer trace-bearer-0003", "X-Forwarded-For": "198.51.100.99",
            "Cookie": "session=trace-cookie-0004"})
        c.get("/api/v1/models?leak=trace-query-0005")
        c.delete("/api/v1/cache", headers={"Authorization": "Bearer trace-wrong-admin-0006"})
        c.portal.call(c.app.state.container.recorder.flush)
        spans = spans_of(c)
    answer = r.json()["answer"]
    dump = json.dumps([json.loads(sp.to_json()) for sp in spans])
    forbidden = [*secrets.values(), prompt, "falcon", answer, "trace-bearer-0003", "198.51.100.99", "testclient",
                 "trace-cookie-0004", "trace-query-0005", "trace-wrong-admin-0006", "mock simulated"]
    assert [f for f in forbidden if f in dump] == []
    for sp in spans:
        assert set(sp.attributes) <= ALLOWED_ATTRIBUTES, (sp.name, set(sp.attributes) - ALLOWED_ATTRIBUTES)
        assert not sp.events  # exceptions are never recorded as events (no messages / stack traces)
        for v in sp.attributes.values():
            assert not isinstance(v, str) or len(v) <= MAX_ATTRIBUTE_LENGTH


def test_unhandled_exception_span_has_type_only():
    app = create_app(traced())

    @app.get("/_explode")
    async def explode():
        raise RuntimeError("secret detail sk-proj-EXPLODESPAN0007 and user text")

    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/_explode")
        spans = spans_of(c)
    http = next(s for s in spans if s.kind.name == "SERVER")
    assert http.status.status_code == StatusCode.ERROR and http.status.description == "RuntimeError"
    assert "EXPLODESPAN0007" not in json.dumps([json.loads(s.to_json()) for s in spans])


def test_safe_attributes_policy():
    from app.classifier.types import ComplexityLevel

    attrs = safe_attributes({
        "llm.model": "m", "chat.complexity": ComplexityLevel.SIMPLE, "not.allowed": "x", "prompt": "secret",
        "llm.retry": 2, "cache.similarity": 0.93, "chat.is_coding": True, "llm.error_kind": None,
        "error.type": "x" * 500, "health.model": "token: sk-proj-ABCDEFGHIJKLMNOP",
    })
    assert attrs["chat.complexity"] == "SIMPLE" and attrs["llm.retry"] == 2 and attrs["cache.similarity"] == 0.93
    assert "not.allowed" not in attrs and "prompt" not in attrs and "llm.error_kind" not in attrs
    assert len(attrs["error.type"]) == MAX_ATTRIBUTE_LENGTH
    assert "ABCDEFGHIJKLMNOP" not in attrs["health.model"]


# --------------------------------------------------------------------------- #
# Exporters, sampling, isolation, concurrency
# --------------------------------------------------------------------------- #
def test_sample_ratio_zero_exports_nothing():
    with TestClient(create_app(traced(tracing_sample_ratio=0.0))) as c:
        assert c.post(CHAT, json=Q).status_code == 200
        assert spans_of(c) == []


def test_otlp_without_optional_package_degrades_safely(caplog):
    with caplog.at_level(logging.WARNING):
        t = Tracing(enabled=True, exporter="otlp")
    assert t.exporter_name == "none" and t.enabled
    assert any("tracing.exporter_unavailable" in r.getMessage() for r in caplog.records)
    t.shutdown()


def test_console_exporter_can_be_configured():
    t = Tracing(enabled=True, exporter="console")
    assert t.exporter_name == "console"
    t.shutdown()


def test_exporter_none_still_creates_trace_ids():
    with TestClient(create_app(traced(tracing_exporter="none"))) as c:
        r = c.post(CHAT, json=Q)
    assert len(r.headers["X-Trace-ID"]) == 32 and c.app.state.container.tracing.memory_exporter is None


def test_tracing_is_isolated_per_app():
    with TestClient(create_app(traced())) as a, TestClient(create_app(traced())) as b:
        a.post(CHAT, json=Q)
        assert spans_of(a) and spans_of(b) == []
        assert a.app.state.container.tracing.provider is not b.app.state.container.tracing.provider


def test_concurrent_requests_get_independent_traces():
    app = create_app(traced())
    n = 50

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            return await asyncio.gather(*[client.post(CHAT, json={"query": f"What is {i} plus {i}?"}) for i in range(n)])

    responses = asyncio.run(go())
    spans = list(app.state.container.tracing.memory_exporter.get_finished_spans())
    trace_ids = {r.headers["X-Trace-ID"] for r in responses}
    assert len(trace_ids) == n
    by_id = {s.context.span_id: s for s in spans}
    for chat in (s for s in spans if s.name == "chat"):
        parent = by_id[chat.parent.span_id]
        assert parent.kind.name == "SERVER" and parent.context.trace_id == chat.context.trace_id
    assert sum(1 for s in spans if s.name == "chat") == n
    assert {hexid(s.context.trace_id, 32) for s in spans} == trace_ids  # no span leaked into another trace


def test_metrics_unaffected_by_tracing():
    with TestClient(create_app(traced())) as c:
        c.post(CHAT, json=Q)
        text = c.get("/metrics").text
    assert 'llm_router_http_requests_total{method="POST",route="/api/v1/chat",status="200"} 1.0' in text
