"""HTTP tests for POST /api/v1/chat and GET /api/v1/models."""

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app
from app.routing.factory import build_chat_service

URL = "/api/v1/chat"


def test_chat_success_full_contract(client):
    r = client.post(URL, json={"query": "Explain Python variables"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {
        "request_id", "answer", "classification", "selected_model", "model_used", "provider",
        "routing", "fallback", "usage", "cost", "latency_ms", "provider_status", "mock_mode",
    }
    assert body["request_id"] == r.headers["X-Request-ID"]
    assert body["answer"].startswith("[MOCK:mock-fast]")
    assert body["classification"]["complexity"] == "SIMPLE"
    assert body["selected_model"] == body["model_used"] == "mock-fast"
    assert body["provider"] == "mock" and body["mock_mode"] is True
    assert body["routing"]["reason"].startswith("Query classified as SIMPLE")
    assert body["fallback"]["used"] is False
    assert body["usage"]["source"] == "estimated"
    assert body["cost"]["pricing_is_placeholder"] is True and body["cost"]["is_estimate"] is True
    assert body["provider_status"] == "healthy"


def test_chat_is_deterministic_in_mock_mode(client):
    a = client.post(URL, json={"query": "Explain Python variables"}).json()
    b = client.post(URL, json={"query": "Explain Python variables"}).json()
    assert a["answer"] == b["answer"] and a["request_id"] != b["request_id"]


@pytest.mark.parametrize(
    "query, tier, model_id",
    [
        ("What is the capital of France?", "SIMPLE", "mock-fast"),
        ("Compare REST and GraphQL for a mobile app backend.", "MEDIUM", "mock-balanced"),
        ("Write a Python function that parses a CSV file and returns the average of each column, handling missing values.", "MEDIUM", "mock-coder"),
        ("Design a distributed cache with sharding and replication. Compare consistency models and analyze the trade-offs.", "COMPLEX", "mock-advanced"),
    ],
)
def test_chat_routes_by_classifier(client, query, tier, model_id):
    body = client.post(URL, json={"query": query}).json()
    assert body["classification"]["complexity"] == tier
    assert body["model_used"] == model_id


def test_chat_classification_matches_classify_endpoint(client):
    q = {"query": "Compare REST and GraphQL for a mobile app backend."}
    assert client.post(URL, json=q).json()["classification"] == client.post("/api/v1/classify", json=q).json()


def test_chat_valid_override(client):
    body = client.post(URL, json={"query": "hi", "model": "mock-advanced", "temperature": 0.5, "max_output_tokens": 50}).json()
    assert body["model_used"] == "mock-advanced" and body["routing"]["override"] is True


@pytest.mark.parametrize("model, code", [("not-a-model", "unknown_model"), ("mock-legacy", "model_disabled")])
def test_chat_rejects_bad_override(client, model, code):
    r = client.post(URL, json={"query": "hi", "model": model})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == code


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 10_001},
        {"query": "hi", "temperature": 3},
        {"query": "hi", "temperature": -0.1},
        {"query": "hi", "max_output_tokens": 0},
        {"query": "hi", "max_output_tokens": 100_000},
        {"query": "hi", "model": "bad model id!"},
        {"query": "hi", "api_key": "sk-injected"},
    ],
)
def test_chat_validation(client, payload):
    r = client.post(URL, json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_incoming_request_id_is_propagated(client):
    r = client.post(URL, json={"query": "hi"}, headers={"X-Request-ID": "client-abc.123"})
    assert r.headers["X-Request-ID"] == "client-abc.123" and r.json()["request_id"] == "client-abc.123"


def test_unsafe_request_id_is_replaced(client):
    r = client.post(URL, json={"query": "hi"}, headers={"X-Request-ID": "bad id\nwith newline"})
    assert r.headers["X-Request-ID"] != "bad id\nwith newline" and len(r.headers["X-Request-ID"]) == 32


def _client(**settings_kw) -> TestClient:
    settings = Settings(_env_file=None, llm_mock_mode=True, provider_retry_backoff_seconds=0, **settings_kw)
    return TestClient(create_app(settings), raise_server_exceptions=False)


def test_chat_fallback_via_mock_failures():
    with _client(mock_failures={"mock-fast": "unavailable"}) as c:
        body = c.post(URL, json={"query": "What is the capital of France?"}).json()
    assert body["selected_model"] == "mock-fast" and body["model_used"] == "mock-balanced"
    assert body["fallback"]["used"] is True
    assert body["fallback"]["original_model"] == "mock-fast" and body["fallback"]["final_model"] == "mock-balanced"
    assert [a["outcome"] for a in body["fallback"]["attempts"]] == ["error", "error", "success"]


def test_chat_all_models_failed_returns_503_without_internals():
    every = {m: "unavailable" for m in ["mock-fast", "mock-balanced", "mock-coder", "mock-advanced"]}
    with _client(mock_failures=every, provider_max_retries=0) as c:
        r = c.post(URL, json={"query": "What is the capital of France?"})
    assert r.status_code == 503
    err = r.json()["error"]
    assert err["code"] == "all_models_failed"
    assert {"model", "outcome", "error_kind"} == set(err["details"]["attempts"][0])
    assert "Traceback" not in r.text and "mock simulated" not in r.text


def test_health_persists_across_requests():
    with _client(mock_failures={"mock-fast": "unavailable"}, provider_max_retries=0, health_failure_threshold=2) as c:
        for _ in range(2):
            c.post(URL, json={"query": "What is the capital of France?"})
        body = c.post(URL, json={"query": "What is the capital of France?"}).json()
        models = {m["id"]: m for m in c.get("/api/v1/models").json()["models"]}
    # Circuit is open: routing now goes straight to the next SIMPLE-capable tier.
    assert body["selected_model"] == "mock-balanced" and body["fallback"]["used"] is False
    assert models["mock-fast"]["health"]["state"] == "unhealthy"


def test_models_endpoint_lists_registry_without_secrets():
    secret = "sk-test-should-never-appear-123"
    with _client(openai_api_key=SecretStr(secret)) as c:
        r = c.get("/api/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["mock_mode"] is True
    ids = {m["id"]: m for m in body["models"]}
    assert set(ids) == {"mock-fast", "mock-balanced", "mock-coder", "mock-advanced", "mock-legacy"}
    assert ids["mock-legacy"]["enabled"] is False
    assert all(m["pricing_is_placeholder"] for m in body["models"])
    assert secret not in r.text and "api_key" not in r.text and "api_base" not in r.text


def test_api_key_never_in_chat_response_or_logs(caplog):
    secret = "sk-test-should-never-appear-456"
    with caplog.at_level("DEBUG"), _client(openai_api_key=SecretStr(secret)) as c:
        r = c.post(URL, json={"query": "What is the capital of France?"})
    assert secret not in r.text
    assert not any(secret in rec.getMessage() for rec in caplog.records)


def test_settings_repr_hides_keys():
    s = Settings(_env_file=None, anthropic_api_key="sk-ant-secret-value-789")
    assert "sk-ant-secret-value-789" not in repr(s) and "sk-ant-secret-value-789" not in json.dumps(s.model_dump(mode="json"))


def test_blank_api_key_treated_as_unset():
    assert "openai" not in Settings(_env_file=None, openai_api_key="  ").provider_api_keys()


def test_mock_mode_never_builds_real_provider():
    service = build_chat_service(Settings(_env_file=None, llm_mock_mode=True))
    for m in service.policy.registry.all():
        assert service.providers.for_model(m).name == "mock"
    assert service.providers._real is None


def test_chat_and_models_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/api/v1/chat"] and "get" in paths["/api/v1/models"]
    assert "/api/v1/classify" in paths and "/health" in paths  # Phase 1/2 regression


def test_phase1_and_phase2_endpoints_unchanged(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/api/v1/health").status_code == 200
    assert client.post("/api/v1/classify", json={"query": "Hello!"}).json()["complexity"] == "SIMPLE"
    assert client.get("/nope").json() == {"error": {"code": "not_found", "message": "Not Found"}}
