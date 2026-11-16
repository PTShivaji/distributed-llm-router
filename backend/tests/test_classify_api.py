"""HTTP-level tests for POST /api/v1/classify."""

import pytest

from app.core.config import Settings
from app.main import create_app

URL = "/api/v1/classify"


def test_classify_simple(client):
    r = client.post(URL, json={"query": "What is the capital of France?"})

    assert r.status_code == 200
    body = r.json()
    assert body["complexity"] == "SIMPLE"
    assert body["intent"]["primary"] == "question_answering"
    assert body["technical"]["is_technical"] is False
    assert body["classifier_version"] == "rule-based-1.0"


def test_classify_complex_full_schema(client):
    query = (
        "Design a fault-tolerant distributed cache with sharding and replication. "
        "Compare consistency models and analyze the trade-offs."
    )
    body = client.post(URL, json={"query": query}).json()

    assert body["complexity"] == "COMPLEX"
    assert set(body) == {
        "complexity", "confidence", "complexity_score", "thresholds", "intent",
        "reasoning", "technical", "features", "explanation", "classifier_version",
    }
    assert 0 <= body["confidence"] <= 1
    assert body["thresholds"] == {"simple_max": 0.28, "complex_min": 0.55}
    assert body["intent"]["primary"] == "system_design"
    assert body["reasoning"]["estimated_steps"] > 1
    assert all({"name", "weight", "detail"} == set(s) for s in body["reasoning"]["signals"])
    assert "systems" in body["technical"]["domains"]
    assert body["explanation"].startswith("Classified as COMPLEX")


def test_classify_coding_query(client):
    body = client.post(URL, json={"query": "Fix this:\n```python\nprint(x\n```\nSyntaxError: invalid syntax"}).json()
    assert body["technical"]["is_coding"] is True
    assert body["features"]["has_code_block"] is True
    assert body["intent"]["primary"] == "debugging"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 123},
        {"query": "x" * 10_001},
        {"query": "hi", "unexpected": True},
    ],
)
def test_invalid_payload_returns_422_envelope(client, payload):
    r = client.post(URL, json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_malformed_json_returns_422(client):
    r = client.post(URL, content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_get_not_allowed(client):
    r = client.get(URL)
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"


def test_only_versioned_path_exists(client):
    assert client.post("/classify", json={"query": "hi"}).status_code == 404


def test_thresholds_come_from_settings():
    from fastapi.testclient import TestClient

    from app.core.config import get_settings

    settings = Settings(
        _env_file=None, environment="test",
        classifier_simple_threshold=0.01, classifier_complex_threshold=0.02,
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        body = c.post(URL, json={"query": "Compare REST and GraphQL"}).json()
    assert body["complexity"] == "COMPLEX"
    assert body["thresholds"] == {"simple_max": 0.01, "complex_min": 0.02}


def test_settings_reject_inverted_thresholds():
    with pytest.raises(ValueError):
        Settings(_env_file=None, classifier_simple_threshold=0.6, classifier_complex_threshold=0.4)


def test_classify_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "post" in paths[URL]
