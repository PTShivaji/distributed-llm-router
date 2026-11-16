"""Phase 5 step 7: production configuration, API security, error sanitisation."""

import json
import logging
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import PROJECT_ROOT, Settings
from app.core.production import ProductionConfigError, check_production_settings, enforce_production_settings
from app.main import create_app

CHAT = "/api/v1/chat"
Q = {"query": "What is the capital of France?"}
PHASE4_ENV = ("REDIS_ENABLED", "DATABASE_ENABLED", "CACHE_ENABLED", "RATE_LIMIT_ENABLED", "HEALTH_BACKEND")


def strong() -> str:
    # Re-draw the (rare) random token that happens to contain a placeholder marker such as "xxx".
    # Exactly one candidate per iteration, checked against EVERY marker before it is returned.
    from app.core.production import PLACEHOLDER_MARKERS

    while True:
        candidate = secrets.token_urlsafe(48)
        lowered = candidate.lower()
        if not any(marker in lowered for marker in PLACEHOLDER_MARKERS):
            return candidate


CHAT_KEY = strong()  # step 8: production requires chat authentication
CHAT_AUTH = {"Authorization": f"Bearer {CHAT_KEY}"}


def prod_settings(**overrides) -> Settings:
    """A fully valid production configuration (random secrets, mock provider, no Redis/DB I/O)."""
    base = dict(
        _env_file=None, environment="production", llm_mock_mode=True, mock_failures={}, log_level="WARNING",
        admin_api_token=strong(), observability_api_token=strong(), client_id_hash_key=strong(),
        postgres_password=strong(), redis_password=strong(), redis_enabled=False, database_enabled=False,
        cache_enabled=False, rate_limit_enabled=False, health_backend="memory",
        # step 8: production requires chat authentication
        chat_auth_mode="api_key", chat_api_keys={"prod-client": CHAT_KEY},
    )
    base.update(overrides)
    return Settings(**base)


def dev_settings(**overrides) -> Settings:
    base = dict(_env_file=None, environment="test", llm_mock_mode=True, mock_failures={}, log_level="WARNING",
                redis_enabled=False, database_enabled=False, cache_enabled=False, rate_limit_enabled=False,
                health_backend="memory")
    base.update(overrides)
    return Settings(**base)


def errors_of(s: Settings) -> set[str]:
    return {i.setting for i in check_production_settings(s) if i.severity == "error"}


# =========================================================================== #
# 1. Production startup validation
# =========================================================================== #
def test_development_defaults_unchanged_and_need_no_secrets():
    s = Settings(_env_file=None)
    assert s.environment == "development" and s.llm_mock_mode is True and s.docs_effective is True
    assert enforce_production_settings(s) == []
    with TestClient(create_app(dev_settings())) as c:
        assert c.get("/health").status_code == 200


def test_production_without_secrets_refuses_to_start():
    s = Settings(_env_file=None, environment="production", redis_enabled=True, database_enabled=True,
                 log_level="WARNING")
    with pytest.raises(ProductionConfigError) as info:
        create_app(s)
    names = {i.setting for i in info.value.issues}
    assert {"ADMIN_API_TOKEN", "OBSERVABILITY_API_TOKEN", "CLIENT_ID_HASH_KEY", "POSTGRES_PASSWORD",
            "REDIS_PASSWORD"} <= names
    assert "Refusing to start" in str(info.value)


def test_valid_production_configuration_starts_with_docs_off():
    with TestClient(create_app(prod_settings())) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/docs").status_code == 404
        assert c.get("/redoc").status_code == 404
        assert c.get("/openapi.json").status_code == 404
        assert c.post(CHAT, json=Q).status_code == 401  # step 8: chat requires a key in production
        assert c.post(CHAT, json=Q, headers=CHAT_AUTH).status_code == 200


@pytest.mark.parametrize("field, value, setting", [
    ("admin_api_token", "short-token", "ADMIN_API_TOKEN"),
    ("admin_api_token", "<generate-random-48-char-token-here-please>", "ADMIN_API_TOKEN"),
    ("observability_api_token", "change-me-change-me-change-me-change-me", "OBSERVABILITY_API_TOKEN"),
    ("client_id_hash_key", "a" * 64, "CLIENT_ID_HASH_KEY"),
    ("postgres_password", "router_password", "POSTGRES_PASSWORD"),
    ("postgres_password", "short", "POSTGRES_PASSWORD"),
    ("redis_password", "my-redis-password-123", "REDIS_PASSWORD"),
])
def test_weak_or_placeholder_secrets_rejected(field, value, setting):
    s = prod_settings(**{field: value, "redis_enabled": True, "database_enabled": True})
    assert setting in errors_of(s)


@pytest.mark.parametrize("overrides, setting", [
    ({"debug": True}, "DEBUG"),
    ({"cors_allowed_origins": ["*"]}, "CORS_ALLOWED_ORIGINS"),
    ({"trust_proxy_headers": True, "trusted_proxies": []}, "TRUSTED_PROXIES"),
    ({"trust_proxy_headers": True, "trusted_proxies": ["0.0.0.0/0"]}, "TRUSTED_PROXIES"),
    ({"cache_semantic_allow_lexical": True}, "CACHE_SEMANTIC_ALLOW_LEXICAL"),
])
def test_unsafe_switches_rejected_in_production(overrides, setting):
    assert setting in errors_of(prod_settings(**overrides))


def test_disabled_services_do_not_require_their_passwords():
    s = prod_settings(postgres_password="router_password", redis_password=None,
                      redis_enabled=False, database_enabled=False)
    assert not ({"POSTGRES_PASSWORD", "REDIS_PASSWORD"} & errors_of(s))


def test_staging_logs_but_does_not_abort(caplog):
    s = Settings(_env_file=None, environment="staging", redis_enabled=False, database_enabled=False,
                 log_level="WARNING")
    with caplog.at_level(logging.WARNING):
        issues = enforce_production_settings(s)
    assert any(i.severity == "error" for i in issues)  # reported...
    assert any(getattr(r, "event", None) == "config.production_check" for r in caplog.records)  # ...as warnings


def test_production_errors_and_logs_never_contain_secret_values(caplog):
    leaked = {"admin_api_token": "Adm1n-weak-but-unique-VALUE",
              "observability_api_token": "0bs-weak-but-unique-VALUE",
              "postgres_password": "Pg-weak-unique-VALUE", "redis_password": "Rd-weak-unique-VALUE"}
    s = prod_settings(**leaked, redis_enabled=True, database_enabled=True)
    with caplog.at_level(logging.DEBUG), pytest.raises(ProductionConfigError) as info:
        create_app(s)
    text = str(info.value) + repr(info.value.issues) + "\n".join(r.getMessage() for r in caplog.records)
    for value in leaked.values():
        assert value not in text
    assert "too short" in str(info.value)


def test_production_warnings_for_recommended_hardening():
    issues = {i.setting: i.severity for i in check_production_settings(prod_settings(
        docs_enabled=True, rate_limit_enabled=True, rate_limit_fail_open=True, persist_prompts=True))}
    for setting in ("DOCS_ENABLED", "HSTS_ENABLED", "RATE_LIMIT_FAIL_OPEN", "PERSIST_PROMPTS/PERSIST_RESPONSES",
                    "LLM_MOCK_MODE"):
        assert issues.get(setting) == "warning", setting


# --- example env files (parsed with the real Settings class; never the root .env) ---
def test_env_example_parses_as_development(monkeypatch):
    for key in PHASE4_ENV:
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=PROJECT_ROOT / ".env.example")
    assert s.environment == "development" and s.llm_mock_mode is True and s.cache_semantic_enabled is False
    assert s.max_request_body_bytes == 1_048_576 and s.cors_allowed_origins == []
    assert enforce_production_settings(s) == []


def test_production_template_is_rejected_until_placeholders_are_replaced(monkeypatch):
    for key in PHASE4_ENV:
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=PROJECT_ROOT / ".env.production.example")
    assert s.environment == "production" and s.rate_limit_fail_open is False and s.docs_effective is False
    assert {"ADMIN_API_TOKEN", "OBSERVABILITY_API_TOKEN", "CLIENT_ID_HASH_KEY", "POSTGRES_PASSWORD",
            "REDIS_PASSWORD"} <= errors_of(s)


def test_example_files_contain_no_real_looking_secrets():
    for name in (".env.example", ".env.production.example"):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert "sk-" not in text.replace("sk-proj", "") and "AIza" not in text and "gsk_" not in text, name


# =========================================================================== #
# 2. Admin and observability authentication
# =========================================================================== #
OBS_PATHS = ["/metrics", "/api/v1/analytics/summary", "/api/v1/cache/stats", "/health/dependencies",
             "/api/v1/health/dependencies"]


@pytest.mark.parametrize("path", OBS_PATHS)
def test_observability_endpoints_require_token_when_configured(path):
    token = strong()
    with TestClient(create_app(dev_settings(observability_api_token=token))) as c:
        missing = c.get(path)
        wrong = c.get(path, headers={"Authorization": "Bearer nope"})
        basic = c.get(path, headers={"Authorization": f"Basic {token}"})
        in_query = c.get(f"{path}?token={token}")
        ok = c.get(path, headers={"Authorization": f"Bearer {token}"})
    for r in (missing, wrong, basic, in_query):
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
        assert r.headers["WWW-Authenticate"] == "Bearer"
    assert ok.status_code in (200, 503)  # 503 = analytics without a database: auth passed
    assert token not in missing.text + ok.text


@pytest.mark.parametrize("path", OBS_PATHS)
def test_observability_endpoints_open_in_development_without_token(path):
    with TestClient(create_app(dev_settings())) as c:
        assert c.get(path).status_code in (200, 503)


def test_observability_endpoints_protected_in_production():
    s = prod_settings()
    with TestClient(create_app(s)) as c:
        codes = {p: c.get(p).status_code for p in OBS_PATHS}
        authed = c.get("/metrics", headers={"Authorization": f"Bearer {s.observability_api_token.get_secret_value()}"})
    assert set(codes.values()) == {401}
    assert authed.status_code == 200


def test_public_endpoints_stay_public_in_production():
    with TestClient(create_app(prod_settings())) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/api/v1/health").status_code == 200
        assert c.get("/api/v1/models").status_code == 200
        assert c.post("/api/v1/classify", json=Q).status_code == 200
        # step 8: /chat is authenticated in production (it can spend money); still reachable with a key
        assert c.post(CHAT, json=Q).status_code == 401
        assert c.post(CHAT, json=Q, headers=CHAT_AUTH).status_code == 200


def test_admin_endpoint_auth():
    token = strong()
    with TestClient(create_app(dev_settings(admin_api_token=token))) as c:
        missing = c.delete("/api/v1/cache")
        wrong = c.delete("/api/v1/cache", headers={"Authorization": "Bearer wrong"})
        obs_token_not_enough = c.delete("/api/v1/cache", headers={"Authorization": f"Basic {token}"})
        ok = c.delete("/api/v1/cache", headers={"Authorization": f"Bearer {token}"})
    assert missing.status_code == wrong.status_code == obs_token_not_enough.status_code == 401
    assert ok.status_code == 503 and ok.json()["error"]["code"] == "cache_unavailable"  # authorised; cache off here


def test_admin_endpoint_disabled_without_token():
    with TestClient(create_app(dev_settings())) as c:
        r = c.delete("/api/v1/cache", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_disabled"


def test_admin_token_is_not_accepted_for_observability():
    admin, obs = strong(), strong()
    with TestClient(create_app(dev_settings(admin_api_token=admin, observability_api_token=obs))) as c:
        assert c.get("/metrics", headers={"Authorization": f"Bearer {admin}"}).status_code == 401
        assert c.delete("/api/v1/cache", headers={"Authorization": f"Bearer {obs}"}).status_code == 401


# =========================================================================== #
# 3. CORS
# =========================================================================== #
ORIGIN = "https://app.example.org"


def test_no_cors_headers_by_default():
    with TestClient(create_app(dev_settings())) as c:
        simple = c.get("/health", headers={"Origin": ORIGIN})
        preflight = c.options(CHAT, headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in simple.headers
    assert "access-control-allow-origin" not in preflight.headers


def test_cors_allows_only_configured_origins():
    with TestClient(create_app(dev_settings(cors_allowed_origins=[ORIGIN]))) as c:
        ok = c.options(CHAT, headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
                                      "Access-Control-Request-Headers": "content-type,authorization"})
        evil = c.options(CHAT, headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "POST"})
        simple = c.post(CHAT, json=Q, headers={"Origin": ORIGIN})
        simple_evil = c.post(CHAT, json=Q, headers={"Origin": "https://evil.example.com"})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in ok.headers
    assert evil.status_code == 400 and "access-control-allow-origin" not in evil.headers
    assert simple.headers["access-control-allow-origin"] == ORIGIN
    assert "x-request-id" in simple.headers["access-control-expose-headers"].lower()
    assert "access-control-allow-origin" not in simple_evil.headers


# =========================================================================== #
# 4. Security headers
# =========================================================================== #
EXPECTED = {"x-content-type-options": "nosniff", "x-frame-options": "DENY", "referrer-policy": "no-referrer",
            "cross-origin-opener-policy": "same-origin", "cross-origin-resource-policy": "same-origin"}


def _assert_secure(r):
    for k, v in EXPECTED.items():
        assert r.headers.get(k) == v, (k, r.status_code)
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert "permissions-policy" in r.headers


def test_security_headers_on_all_kinds_of_responses():
    app = create_app(dev_settings(max_request_body_bytes=2048))

    @app.get("/_boom")
    async def boom():
        raise RuntimeError("x")

    with TestClient(app, raise_server_exceptions=False) as c:
        responses = {
            "ok": c.get("/health"), "chat": c.post(CHAT, json=Q), "422": c.post(CHAT, json={}),
            "404": c.get("/nope"), "405": c.put("/health"),
            "413": c.post(CHAT, content=b"x" * 5000, headers={"content-type": "application/json"}),
            "500": c.get("/_boom"),
        }
    for name, r in responses.items():
        _assert_secure(r)
    assert responses["chat"].headers["cache-control"] == "no-store"
    assert "cache-control" not in responses["ok"].headers or responses["ok"].headers["cache-control"] != "no-store"
    assert "strict-transport-security" not in responses["ok"].headers


def test_hsts_only_when_enabled():
    with TestClient(create_app(dev_settings(hsts_enabled=True, hsts_max_age_seconds=600))) as c:
        r = c.get("/health")
    assert r.headers["strict-transport-security"] == "max-age=600; includeSubDomains"


def test_docs_pages_get_a_docs_compatible_csp_api_stays_strict():
    with TestClient(create_app(dev_settings())) as c:
        docs, api = c.get("/docs"), c.get("/openapi.json")
    assert docs.status_code == 200 and "cdn.jsdelivr.net" in docs.headers["content-security-policy"]
    assert api.headers["content-security-policy"].startswith("default-src 'none'")


def test_security_headers_can_be_disabled():
    with TestClient(create_app(dev_settings(security_headers_enabled=False))) as c:
        assert "x-frame-options" not in c.get("/health").headers


# =========================================================================== #
# 5. Request-size enforcement
# =========================================================================== #
def _app_with_limit(limit=4096, **kw):
    return create_app(dev_settings(max_request_body_bytes=limit, **kw))


def test_declared_oversized_body_rejected_without_reading():
    app = _app_with_limit()
    with TestClient(app) as c:
        r = c.post(CHAT, content=b'{"query":"' + b"a" * 10_000 + b'"}', headers={"content-type": "application/json"})
        metrics = c.get("/metrics").text
    assert r.status_code == 413 and r.json()["error"]["code"] == "request_too_large"
    assert len(r.content) < 300  # nothing echoed back
    assert sum(app.state.container.chat_service.providers.mock.calls.values()) == 0
    assert 'llm_router_errors_total{category="validation",code="request_too_large"} 1.0' in metrics


def test_chunked_body_without_content_length_is_cut_off():
    def body():
        yield b'{"query":"'
        for _ in range(200):
            yield b"a" * 100  # 20 KB, streamed with no Content-Length
        yield b'"}'

    with TestClient(_app_with_limit()) as c:
        r = c.post(CHAT, content=body(), headers={"content-type": "application/json"})
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413 and r.json()["error"]["code"] == "request_too_large"


def test_body_at_the_limit_is_accepted():
    payload = json.dumps({"query": "What is the capital of France?"}).encode()
    with TestClient(_app_with_limit(limit=len(payload) + 2048)) as c:  # fixed overhead below the limit
        assert c.post(CHAT, content=payload, headers={"content-type": "application/json"}).status_code == 200
    with TestClient(_app_with_limit(limit=1024)) as c:
        exact = c.post(CHAT, content=payload + b" " * (1024 - len(payload)), headers={"content-type": "application/json"})
        over = c.post(CHAT, content=payload + b" " * (1025 - len(payload)), headers={"content-type": "application/json"})
    assert exact.status_code == 200 and over.status_code == 413


def test_default_limit_is_one_mebibyte_and_old_5mb_echo_is_gone():
    with TestClient(create_app(dev_settings())) as c:
        r = c.post(CHAT, content=b'{"query":"' + b"A" * 5_000_000 + b'"}', headers={"content-type": "application/json"})
    assert r.status_code == 413 and len(r.content) < 300  # previously: 422 echoing ~5,000,227 bytes


# =========================================================================== #
# 6. Error-response sanitisation
# =========================================================================== #
def test_validation_errors_do_not_echo_submitted_values():
    secret = "sk-proj-ECHOEDBACK1234567890"
    with TestClient(create_app(dev_settings())) as c:
        extra = c.post(CHAT, json={"query": "hi", "api_key": secret})
        too_long = c.post(CHAT, json={"query": "Q" * 10_001})
        bad_type = c.post(CHAT, json={"query": "hi", "temperature": "PRIVATE-VALUE"})
        malformed = c.post(CHAT, content=b'{"query": "unterminated PRIVATE', headers={"content-type": "application/json"})
    for r in (extra, too_long, bad_type, malformed):
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
        for d in r.json()["error"]["details"]:
            assert set(d) <= {"type", "loc", "msg", "ctx"}
    assert secret not in extra.text
    assert "QQQQQQQQ" not in too_long.text and len(too_long.content) < 1000
    assert "PRIVATE-VALUE" not in bad_type.text and "PRIVATE" not in malformed.text
    assert too_long.json()["error"]["details"][0]["ctx"] == {"max_length": 10000}  # useful primitive kept
    assert extra.json()["error"]["details"][0]["loc"] == ["body", "api_key"]


def test_unexpected_exceptions_are_generic():
    app = create_app(dev_settings())

    @app.get("/_explode")
    async def explode():
        raise RuntimeError("internal detail sk-proj-INTERNALSECRET999 at /srv/app/secret.py line 42")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/_explode")
    assert r.status_code == 500
    assert r.json() == {"error": {"code": "internal_error", "message": "An unexpected error occurred"}}
    assert "INTERNALSECRET" not in r.text and "Traceback" not in r.text and r.headers["x-request-id"]


def test_errors_never_reveal_settings_or_config():
    with TestClient(create_app(dev_settings(admin_api_token=strong()))) as c:
        texts = [c.delete("/api/v1/cache").text, c.get("/nope").text, c.post(CHAT, json={"model": "x"}).text]
    for t in texts:
        assert "router_password" not in t and "postgresql" not in t and "redis://" not in t


# =========================================================================== #
# 7. Rate limiting (isolated Redis) and API compatibility
# =========================================================================== #
def test_rate_limit_429_is_sanitised_and_secured(infra_settings):
    with TestClient(create_app(infra_settings(rate_limit_per_minute=1, rate_limit_per_hour=0,
                                              cache_enabled=False))) as c:
        c.post(CHAT, json=Q, headers={"X-Forwarded-For": "203.0.113.50"})
        r = c.post(CHAT, json=Q, headers={"X-Forwarded-For": "203.0.113.51"})  # spoofing does not help
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limit_exceeded"
    _assert_secure(r)
    assert r.headers["retry-after"] and "203.0.113" not in r.text and "testclient" not in r.text


def test_api_contracts_unchanged():
    with TestClient(create_app(dev_settings())) as c:
        chat = c.post(CHAT, json=Q).json()
        classify = c.post("/api/v1/classify", json=Q).json()
        health = c.get("/health").json()
        paths = set(c.get("/openapi.json").json()["paths"])
    assert {"request_id", "answer", "classification", "selected_model", "model_used", "provider", "routing",
            "fallback", "usage", "cost", "latency_ms", "provider_status", "mock_mode", "cache"} <= set(chat)
    assert {"complexity", "confidence", "intent", "reasoning", "technical", "explanation"} <= set(classify)
    assert health["status"] == "ok"
    assert {"/health", "/api/v1/health", "/api/v1/classify", "/api/v1/chat", "/api/v1/models",
            "/api/v1/cache/stats", "/api/v1/cache", "/api/v1/analytics/summary", "/metrics",
            "/health/dependencies"} <= paths
