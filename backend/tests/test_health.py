"""Tests for health endpoints, configuration and error handling."""

from datetime import datetime

import pytest
from fastapi import Query

from app.core.config import Settings, get_settings
from app.core.errors import AppError

HEALTH_PATHS = ["/health", "/api/v1/health"]


@pytest.fixture(autouse=True)
def _override_settings(app, test_settings):
    # Ensure the Depends(get_settings) in the route uses the test settings.
    app.dependency_overrides[get_settings] = lambda: test_settings
    yield
    app.dependency_overrides.clear()


# --- Health endpoints -------------------------------------------------------

@pytest.mark.parametrize("path", HEALTH_PATHS)
def test_health_returns_ok(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_name"] == "Test Router"
    assert body["version"] == "9.9.9"
    assert body["environment"] == "test"
    assert body["uptime_seconds"] >= 0
    datetime.fromisoformat(body["timestamp"])  # valid ISO-8601


@pytest.mark.parametrize("path", HEALTH_PATHS)
def test_health_rejects_wrong_method(client, path):
    response = client.post(path)

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_health_listed_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/health" in paths
    assert "/api/v1/health" in paths


# --- Error handling ---------------------------------------------------------

def test_unknown_route_returns_json_404(client):
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_unhandled_exception_returns_generic_500(app, client):
    @app.get("/_boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    response = client.get("/_boom")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert "secret" not in error["message"]  # internals are not leaked


def test_app_error_uses_custom_status_and_code(app, client):
    class UpstreamUnavailable(AppError):
        status_code = 503
        code = "upstream_unavailable"

    @app.get("/_app_error")
    async def app_error():
        raise UpstreamUnavailable("Provider down", details={"provider": "x"})

    response = client.get("/_app_error")

    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "upstream_unavailable", "message": "Provider down", "details": {"provider": "x"}}
    }


def test_validation_error_envelope(app, client):
    @app.get("/_validate")
    async def validate(n: int = Query(...)):
        return {"n": n}

    response = client.get("/_validate", params={"n": "not-an-int"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert isinstance(error["details"], list)


# --- Configuration ----------------------------------------------------------

def test_settings_defaults_require_no_api_keys():
    s = Settings(_env_file=None)
    assert s.environment == "development"
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.database_url == "postgresql+psycopg://router:router_password@localhost:5433/llm_router"


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("APP_NAME", "From Env")
    monkeypatch.setenv("REDIS_PASSWORD", "pw")
    monkeypatch.setenv("POSTGRES_PORT", "6543")

    s = Settings(_env_file=None)

    assert s.app_name == "From Env"
    assert s.redis_url == "redis://:pw@localhost:6379/0"
    assert ":6543/" in s.database_url


def test_settings_reject_invalid_port(monkeypatch):
    monkeypatch.setenv("PORT", "99999")
    with pytest.raises(ValueError):
        Settings(_env_file=None)
