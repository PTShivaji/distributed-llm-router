"""Phase 5 step 3: Prometheus metrics - values, labels, endpoint, auth, isolation, concurrency."""

import asyncio
import math
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app.core.config import Settings
from app.main import create_app
from app.observability.metrics import CACHE_STATUSES, Metrics

CHAT = "/api/v1/chat"
Q = {"query": "What is the capital of France?"}
MOCK_MODELS = {"mock-fast", "mock-balanced", "mock-coder", "mock-advanced", "mock-legacy"}


def hermetic(**overrides) -> Settings:
    base = dict(_env_file=None, llm_mock_mode=True, mock_failures={}, redis_enabled=False, database_enabled=False,
                cache_enabled=False, rate_limit_enabled=False, health_backend="memory",
                provider_retry_backoff_seconds=0, log_level="WARNING")
    base.update(overrides)
    return Settings(**base)


def scrape(c: TestClient, **headers) -> dict[tuple[str, tuple], float]:
    """{(sample_name, sorted label items): value} from GET /metrics."""
    r = c.get("/metrics", headers=headers)
    assert r.status_code == 200, r.text
    out = {}
    for family in text_string_to_metric_families(r.text):
        for s in family.samples:
            out[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return out


def value(samples, name, **labels) -> float:
    return samples.get((name, tuple(sorted(labels.items()))), 0.0)


def total(samples, name, **match) -> float:
    return sum(v for (n, labels), v in samples.items()
               if n == name and all(dict(labels).get(k) == want for k, want in match.items()))


# --------------------------------------------------------------------------- #
# Endpoint & format
# --------------------------------------------------------------------------- #
def test_metrics_endpoint_format():
    with TestClient(create_app(hermetic())) as c:
        r = c.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    families = {f.name: f for f in text_string_to_metric_families(r.text)}
    assert families["llm_router_build_info"].type == "gauge"
    assert families["llm_router_http_requests"].type == "counter"
    assert families["llm_router_http_request_duration_seconds"].type == "histogram"
    assert "# HELP llm_router_http_requests_total" in r.text and "# TYPE llm_router_http_requests_total counter" in r.text
    assert "_created" not in r.text  # created-timestamp series disabled


def test_metrics_disabled():
    with TestClient(create_app(hermetic(metrics_enabled=False))) as c:
        c.post(CHAT, json=Q)
        r = c.get("/metrics")
    assert r.status_code == 404


def test_metrics_endpoint_listed_outside_api_prefix():
    with TestClient(create_app(hermetic())) as c:
        paths = c.get("/openapi.json").json()["paths"]
    assert "/metrics" in paths and "/api/v1/metrics" not in paths


# --------------------------------------------------------------------------- #
# Authentication (OBSERVABILITY_API_TOKEN)
# --------------------------------------------------------------------------- #
def test_metrics_open_when_no_token_configured():
    with TestClient(create_app(hermetic())) as c:
        assert c.get("/metrics").status_code == 200


def test_metrics_require_token_when_configured():
    with TestClient(create_app(hermetic(observability_api_token="obs-token-123456"))) as c:
        missing = c.get("/metrics")
        wrong = c.get("/metrics", headers={"Authorization": "Bearer nope"})
        basic = c.get("/metrics", headers={"Authorization": "Basic obs-token-123456"})
        ok = c.get("/metrics", headers={"Authorization": "Bearer obs-token-123456"})
        other = c.get("/health")
    assert missing.status_code == wrong.status_code == basic.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized" and missing.headers["WWW-Authenticate"] == "Bearer"
    assert ok.status_code == 200 and "llm_router_build_info" in ok.text
    assert other.status_code == 200  # token only guards /metrics in this step


def test_unauthorized_scrapes_are_counted_without_leaking_token():
    with TestClient(create_app(hermetic(observability_api_token="obs-token-123456"))) as c:
        c.get("/metrics", headers={"Authorization": "Bearer wrong-guess-777"})
        text = c.get("/metrics", headers={"Authorization": "Bearer obs-token-123456"}).text
    assert 'llm_router_errors_total{category="auth",code="unauthorized"} 1.0' in text
    assert "wrong-guess-777" not in text and "obs-token-123456" not in text


# --------------------------------------------------------------------------- #
# HTTP metrics
# --------------------------------------------------------------------------- #
def test_http_request_counts_and_latency():
    with TestClient(create_app(hermetic())) as c:
        for _ in range(3):
            c.post(CHAT, json=Q)
        c.get("/health")
        c.get("/api/v1/models/")  # trailing slash: no such route
        s = scrape(c)
    assert value(s, "llm_router_http_requests_total", method="POST", route="/api/v1/chat", status="200") == 3
    assert value(s, "llm_router_http_requests_total", method="GET", route="/health", status="200") == 1
    assert value(s, "llm_router_http_request_duration_seconds_count", method="POST", route="/api/v1/chat") == 3
    assert value(s, "llm_router_http_request_duration_seconds_sum", method="POST", route="/api/v1/chat") > 0
    assert value(s, "llm_router_http_request_duration_seconds_bucket", method="POST", route="/api/v1/chat", le="+Inf") == 3
    assert value(s, "llm_router_http_requests_in_progress") == 1  # the scrape itself


def test_route_label_is_a_template_and_bounded():
    with TestClient(create_app(hermetic())) as c:
        for i in range(50):
            c.get(f"/scan/{i}/admin.php?id={i}")
        s = scrape(c)
    routes = {dict(labels)["route"] for (n, labels), _ in s.items() if n == "llm_router_http_requests_total"}
    assert routes == {"unmatched"}  # 50 random paths -> ONE series
    assert value(s, "llm_router_http_requests_total", method="GET", route="unmatched", status="404") == 50


def test_unknown_http_method_is_bucketed():
    with TestClient(create_app(hermetic())) as c:
        c.request("PROPFIND", "/health")
        s = scrape(c)
    assert total(s, "llm_router_http_requests_total", method="OTHER") == 1


# --------------------------------------------------------------------------- #
# Error categories
# --------------------------------------------------------------------------- #
def test_error_categories(test_settings):
    app = create_app(hermetic())

    @app.get("/_boom")
    async def boom():
        raise RuntimeError("x")

    with TestClient(app, raise_server_exceptions=False) as c:
        c.get("/nope")
        c.post(CHAT, json={})
        c.post(CHAT, json={"query": "hi", "model": "not-registered"})
        c.post(CHAT, json={"query": "hi", "model": "mock-legacy"})
        c.get("/_boom")
        s = scrape(c)
    assert value(s, "llm_router_errors_total", category="not_found", code="not_found") == 1
    assert value(s, "llm_router_errors_total", category="validation", code="validation_error") == 1
    assert value(s, "llm_router_errors_total", category="client", code="unknown_model") == 1
    assert value(s, "llm_router_errors_total", category="client", code="model_disabled") == 1
    assert value(s, "llm_router_errors_total", category="internal", code="internal_error") == 1
    assert value(s, "llm_router_http_requests_total", method="GET", route="/_boom", status="500") == 1


def test_all_models_failed_is_a_provider_error():
    every = {m: "unavailable" for m in MOCK_MODELS - {"mock-legacy"}}
    with TestClient(create_app(hermetic(mock_failures=every, provider_max_retries=0))) as c:
        assert c.post(CHAT, json=Q).status_code == 503
        s = scrape(c)
    assert value(s, "llm_router_errors_total", category="provider", code="all_models_failed") == 1
    assert value(s, "llm_router_chat_requests_total", complexity="SIMPLE", selected_model="mock-fast",
                 model_used="none", cache_status="disabled", outcome="error") == 1


# --------------------------------------------------------------------------- #
# Provider / routing metrics
# --------------------------------------------------------------------------- #
def test_provider_retry_and_fallback_metrics():
    with TestClient(create_app(hermetic(mock_failures={"mock-fast": "unavailable"}, provider_max_retries=1))) as c:
        body = c.post(CHAT, json=Q).json()
        s = scrape(c)
    assert body["model_used"] == "mock-balanced"
    attempts = "llm_router_provider_attempts_total"
    assert value(s, attempts, model="mock-fast", provider="mock", outcome="error", error_kind="unavailable") == 2
    assert value(s, attempts, model="mock-balanced", provider="mock", outcome="success", error_kind="none") == 1
    assert value(s, "llm_router_retries_total", model="mock-fast") == 1
    assert value(s, "llm_router_fallbacks_total", from_model="mock-fast", to_model="mock-balanced") == 1
    assert value(s, "llm_router_provider_latency_seconds_count", model="mock-fast", provider="mock", outcome="error") == 2
    assert value(s, "llm_router_provider_latency_seconds_count", model="mock-balanced", provider="mock", outcome="success") == 1
    assert value(s, "llm_router_chat_requests_total", complexity="SIMPLE", selected_model="mock-fast",
                 model_used="mock-balanced", cache_status="disabled", outcome="success") == 1


def test_timeout_is_recorded_with_latency(make_service):
    from app.routing.service import ChatCommand
    from tests.conftest import model

    svc = make_service([model("slow", timeout_seconds=0.05, fallbacks=("ok",)), model("ok", priority=500)],
                       failures={"slow": "timeout"})
    asyncio.run(svc.chat(ChatCommand("What is the capital of France?", "r")))
    m = svc.metrics
    assert m.provider_attempts.labels(model="slow", provider="mock", outcome="error", error_kind="timeout")._value.get() == 1
    hist = m.provider_latency.labels(model="slow", provider="mock", outcome="error")
    assert hist._sum.get() >= 0.05


def test_skipped_unhealthy_attempts_counted(make_service):
    from app.routing.health import HealthTracker
    from app.routing.service import ChatCommand
    from tests.conftest import model

    health = HealthTracker(failure_threshold=1, cooldown_seconds=60)
    health.record_failure("sick", "unavailable")
    svc = make_service([model("p", priority=1, fallbacks=("sick", "ok")), model("sick", priority=2), model("ok", priority=3)],
                       failures={"p": "auth"}, health=health, policy_kwargs={"fallback_auto_tier": False})
    asyncio.run(svc.chat(ChatCommand("What is the capital of France?", "r")))
    m = svc.metrics
    assert m.provider_attempts.labels(model="sick", provider="mock", outcome="skipped_unhealthy", error_kind="none")._value.get() == 1
    assert m.fallbacks.labels(from_model="p", to_model="ok")._value.get() == 1
    assert m.fallbacks.labels(from_model="p", to_model="sick")._value.get() == 0  # skipped, not attempted


# --------------------------------------------------------------------------- #
# Usage and cost
# --------------------------------------------------------------------------- #
def test_token_and_cost_metrics_match_response():
    with TestClient(create_app(hermetic())) as c:
        bodies = [c.post(CHAT, json={"query": q}).json() for q in ("What is the capital of France?", "Who wrote Hamlet?")]
        s = scrape(c)
    want_in = sum(b["usage"]["input_tokens"] for b in bodies)
    want_out = sum(b["usage"]["output_tokens"] for b in bodies)
    want_cost = sum(b["cost"]["total_cost"] for b in bodies)
    assert value(s, "llm_router_tokens_total", model="mock-fast", direction="input", source="estimated") == want_in
    assert value(s, "llm_router_tokens_total", model="mock-fast", direction="output", source="estimated") == want_out
    assert value(s, "llm_router_estimated_cost_usd_total", model="mock-fast", pricing="placeholder") == pytest.approx(want_cost)


# --------------------------------------------------------------------------- #
# Cache metrics (isolated Redis namespace)
# --------------------------------------------------------------------------- #
def test_cache_metrics_and_hit_ratio(infra_settings):
    with TestClient(create_app(infra_settings(rate_limit_enabled=False))) as c:
        before = scrape(c)
        c.post(CHAT, json=Q)
        c.post(CHAT, json=Q)
        c.post(CHAT, json={"query": "What is the capital of France?", "use_cache": False})
        s = scrape(c)
    assert math.isnan(value(before, "llm_router_cache_hit_ratio"))  # undefined before any lookup
    assert value(s, "llm_router_cache_lookups_total", status="miss") == 1
    assert value(s, "llm_router_cache_lookups_total", status="hit_exact") == 1
    assert value(s, "llm_router_cache_lookups_total", status="bypass") == 1
    assert value(s, "llm_router_cache_hit_ratio") == 0.5
    # The cache hit consumed no tokens: only the 2 provider calls (miss + bypass) are counted.
    assert value(s, "llm_router_provider_attempts_total", model="mock-fast", provider="mock",
                 outcome="success", error_kind="none") == 2


# --------------------------------------------------------------------------- #
# Rate-limit metrics (isolated Redis namespace)
# --------------------------------------------------------------------------- #
def test_client_rate_limit_rejections(infra_settings):
    with TestClient(create_app(infra_settings(rate_limit_per_minute=1, rate_limit_per_hour=0, cache_enabled=False))) as c:
        c.post(CHAT, json=Q)
        assert c.post(CHAT, json=Q).status_code == 429
        assert c.post(CHAT, json=Q).status_code == 429
        s = scrape(c)
    assert value(s, "llm_router_rate_limit_rejections_total", scope="client", window="minute") == 2
    assert value(s, "llm_router_errors_total", category="rate_limited", code="rate_limit_exceeded") == 2


def test_model_rate_limit_rejections(infra_settings):
    with TestClient(create_app(infra_settings(model_rate_limits={"mock-fast": 1}, rate_limit_enabled=True,
                                              rate_limit_per_minute=100, cache_enabled=False))) as c:
        c.post(CHAT, json=Q)
        second = c.post(CHAT, json=Q).json()
        s = scrape(c)
    assert second["model_used"] != "mock-fast"
    assert value(s, "llm_router_rate_limit_rejections_total", scope="model", window="minute") == 1
    assert value(s, "llm_router_provider_attempts_total", model="mock-fast", provider="mock",
                 outcome="skipped_rate_limited", error_kind="rate_limit") == 1


# --------------------------------------------------------------------------- #
# Label safety: nothing user-controlled ever becomes a label
# --------------------------------------------------------------------------- #
ALLOWED = {
    "model": MOCK_MODELS | {"none", "other"},
    "selected_model": MOCK_MODELS | {"none", "other"},
    "model_used": MOCK_MODELS | {"none", "other"},
    "from_model": MOCK_MODELS | {"none", "other"},
    "to_model": MOCK_MODELS | {"none", "other"},
    "provider": {"mock", "none", "other"},
    "complexity": {"SIMPLE", "MEDIUM", "COMPLEX", "unknown"},
    "cache_status": CACHE_STATUSES | {"other"},
    "status": None,  # numeric HTTP status, checked separately
    "method": {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "OTHER"},
}


def test_no_user_input_in_any_label():
    secret_prompt = "zebra-prompt-unique-91827 What is the capital of France?"
    with TestClient(create_app(hermetic(mock_failures={"mock-fast": "unavailable"}))) as c:
        c.post(CHAT, json={"query": secret_prompt}, headers={"X-Request-ID": "rid-label-check-1",
                                                               "X-Forwarded-For": "198.51.100.23"})
        c.post(CHAT, json={"query": "hi", "model": "attacker-model-name"})
        c.get("/api/v1/models?inject=label-injection-xyz")
        c.get("/../../etc/passwd")
        r = c.get("/metrics")
        app_routes = {getattr(rt, "path", None) for rt in c.app.routes}
    text = r.text
    for leaked in ("zebra-prompt", "rid-label-check-1", "198.51.100.23", "testclient", "attacker-model-name",
                   "label-injection-xyz", "passwd"):
        assert leaked not in text, leaked
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            for name, val in sample.labels.items():
                if name == "route":
                    assert val == "unmatched" or val.startswith(("/api/v1/", "/health", "/metrics", "/docs", "/openapi")), val
                elif name == "status" and sample.name.startswith("llm_router_cache_lookups"):
                    assert val in CACHE_STATUSES | {"other"}, val
                elif name == "status":
                    assert val.isdigit() and 100 <= int(val) <= 599, (sample.name, val)
                elif name in ALLOWED and ALLOWED[name] is not None:
                    assert val in ALLOWED[name], (name, val)
    assert app_routes  # sanity


def test_metrics_facade_bounds_unknown_values():
    m = Metrics(model_ids=["a"], providers=["mock"])
    m.provider_attempt("evil-model", "evil-provider", "weird-outcome", "boom", 0.1)
    m.cache_lookup("strange")
    m.observe_http("BREW", "/x", 418, 0.01, "made-up-category", "made-up-code")
    m.rate_limit_rejected("galaxy", "century")
    from prometheus_client import generate_latest

    text = generate_latest(m.registry).decode()
    for leaked in ("evil-model", "evil-provider", "weird-outcome", "strange", "BREW", "made-up", "galaxy", "century"):
        assert leaked not in text
    assert 'model="other"' in text and 'provider="other"' in text and 'method="OTHER"' in text


# --------------------------------------------------------------------------- #
# Isolation & disabled facade
# --------------------------------------------------------------------------- #
def test_registries_are_isolated_per_app():
    with TestClient(create_app(hermetic())) as a, TestClient(create_app(hermetic())) as b:
        for _ in range(3):
            a.post(CHAT, json=Q)
        sa, sb = scrape(a), scrape(b)
    assert value(sa, "llm_router_http_requests_total", method="POST", route="/api/v1/chat", status="200") == 3
    assert value(sb, "llm_router_http_requests_total", method="POST", route="/api/v1/chat", status="200") == 0
    assert a.app.state.container.metrics.registry is not b.app.state.container.metrics.registry


def test_disabled_metrics_record_nothing():
    m = Metrics(enabled=False)
    m.observe_http("GET", "/x", 200, 0.1, None, None)
    m.provider_attempt("a", "mock", "success", None, 0.1)
    m.cache_lookup("miss")
    m.usage("a", 10, 10, "estimated", 1.0, True)
    from prometheus_client import generate_latest

    text = generate_latest(m.registry).decode()
    assert "llm_router_http_requests_total{" not in text and "llm_router_tokens_total{" not in text


# --------------------------------------------------------------------------- #
# Concurrency
# --------------------------------------------------------------------------- #
def test_concurrent_requests_are_counted_exactly():
    app = create_app(hermetic())
    n = 200

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(*[client.post(CHAT, json={"query": f"What is {i} plus {i}?"}) for i in range(n)])
            metrics_text = (await client.get("/metrics")).text
        return responses, metrics_text

    responses, text = asyncio.run(go())
    assert all(r.status_code == 200 for r in responses)
    samples = {}
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            samples[(s.name, tuple(sorted(s.labels.items())))] = s.value
    assert value(samples, "llm_router_http_requests_total", method="POST", route="/api/v1/chat", status="200") == n
    assert total(samples, "llm_router_chat_requests_total", outcome="success") == n
    assert total(samples, "llm_router_provider_attempts_total", outcome="success") == n
    assert value(samples, "llm_router_http_requests_in_progress") == 1  # only the scrape remains in flight


# --------------------------------------------------------------------------- #
# Overhead (generous bound; precise numbers come from the benchmark step)
# --------------------------------------------------------------------------- #
def test_per_request_metric_overhead_is_small():
    m = Metrics(model_ids=["mock-fast", "mock-balanced"], providers=["mock"])
    n = 2000
    t0 = time.perf_counter()
    for _ in range(n):  # the metric operations of one fallback request
        m.http_in_progress.inc()
        m.cache_lookup("miss")
        m.provider_attempt("mock-fast", "mock", "error", "unavailable", 0.001)
        m.retry("mock-fast")
        m.fallback("mock-fast", "mock-balanced")
        m.provider_attempt("mock-balanced", "mock", "success", None, 0.001)
        m.chat_completed(complexity="SIMPLE", selected_model="mock-fast", model_used="mock-balanced",
                         cache_status="miss", outcome="success")
        m.usage("mock-balanced", 8, 51, "estimated", 0.0002, True)
        m.http_in_progress.dec()
        m.observe_http("POST", "/api/v1/chat", 200, 0.01, None, None)
    per_request_us = (time.perf_counter() - t0) / n * 1e6
    assert per_request_us < 1000, per_request_us  # far below 1 ms even on slow CI
