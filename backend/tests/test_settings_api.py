"""Step 10.4 phase 1: GET /api/v1/settings - read-only, non-secret configuration view.

Every test builds its own isolated Settings(_env_file=None, ...): the developer's .env is
never read (LLMR_DISABLE_ENV_FILE=1 in conftest.py as well). Redis/PostgreSQL stay disabled.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.settings_view import SECRET_ISSUE_MESSAGE, SettingsLeakError, build_settings_view
from app.main import create_app
from app.routing.registry import load_registry
from tests.test_production_hardening import dev_settings, prod_settings, strong

PATH = "/api/v1/settings"

# The complete allow-list, written out independently of the schema module on purpose:
# adding a field to the response must be a deliberate change to this test as well.
ALLOWED = {
    "app": {"name", "version", "environment", "debug", "uptime_seconds"},
    "mode": {"mock_mode", "allow_real_provider_calls", "real_provider_calls_enabled"},
    "routing": {"strategy", "tier_escalation", "model_registry", "fallback_enabled", "fallback_auto_tier",
                "fallback_on_override", "max_fallback_models", "provider_timeout_seconds", "provider_max_retries",
                "provider_retry_backoff_seconds", "default_max_output_tokens", "real_max_output_tokens",
                "health_backend", "health_failure_threshold", "health_cooldown_seconds"},
    "auth": {"chat_auth_mode", "chat_clients_configured", "observability_token_required",
             "admin_endpoints_enabled", "client_id_hashing"},
    "rate_limits": {"enabled", "per_minute", "per_hour", "fail_open", "model_limits", "trust_proxy_headers",
                    "trusted_proxies_configured"},
    "cache": {"enabled", "semantic_enabled", "semantic_allow_lexical", "ttl_seconds", "similarity_threshold",
              "max_entries_per_scope", "embedding_provider", "embedding_model", "embedding_pricing_is_placeholder"},
    "budget": {"configured", "applies_to", "daily_usd", "daily_usd_per_client", "daily_tokens_per_client",
               "max_cost_per_request_usd", "fail_closed", "monthly_limit_supported"},
    "persistence": {"redis_enabled", "database_enabled", "database_auto_migrate", "persist_prompts",
                    "persist_responses"},
    "http": {"docs_enabled", "security_headers_enabled", "hsts_enabled", "hsts_max_age_seconds",
             "max_request_body_bytes", "cors_enabled", "cors_origins_configured"},
    "observability": {"metrics_enabled", "access_log_enabled", "log_level", "log_format", "tracing_enabled",
                      "tracing_exporter", "tracing_sample_ratio", "tracing_trust_incoming"},
}
PROVIDER_FIELDS = {"provider", "supported_for_real_calls", "credential_required", "credential_setting",
                   "credential_configured", "models"}
ISSUE_FIELDS = {"setting", "severity", "message"}

# Unique, recognisable dummy values (never real secrets). Every one of them must stay out of the response.
SENTINEL_SECRETS = {
    "openai_api_key": "sk-SENTINEL-openai-0001-aaaaaaaa",
    "anthropic_api_key": "sk-ant-SENTINEL-anthropic-0002",
    "gemini_api_key": "SENTINEL-gemini-key-0003-bbbbbbbb",
    "groq_api_key": "gsk_SENTINEL-groq-0004-cccccccc",
    "mistral_api_key": "SENTINEL-mistral-key-0005-dddddd",
    "admin_api_token": "SENTINEL-admin-token-0006-eeeeeeeeeeeeeeee",
    "observability_api_token": "SENTINEL-obs-token-0007-ffffffffffffffff",
    "client_id_hash_key": "SENTINEL-hash-key-0008-gggggggggggggggg",
    "redis_password": "SENTINEL-redis-pass-0009",
    "postgres_password": "SENTINEL-pg-pass-0010-hhhh",
}
SENTINEL_CHAT = {"sentinel-client-alpha": "SENTINEL-chat-key-0011-iiiiiiii",
                 "sentinel-client-beta": "SENTINEL-chat-key-0012-jjjjjjjj"}
# Non-secret infrastructure details that must not be exposed either.
SENTINEL_INFRA = {
    "redis_host": "sentinel-redis-host.internal", "postgres_host": "sentinel-pg-host.internal",
    "postgres_user": "sentinel_pg_user", "postgres_db": "sentinel_pg_db",
    "tracing_otlp_endpoint": "http://sentinel-otlp.internal:4318/v1/traces",
}
SENTINEL_ORIGINS = ["https://sentinel-origin-one.example", "https://sentinel-origin-two.example"]
SENTINEL_PROXIES = ["10.99.88.0/24"]


def auth(s: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {s.observability_api_token.get_secret_value()}"}


def sentinel_settings(**overrides) -> Settings:
    base = dict(SENTINEL_SECRETS, **SENTINEL_INFRA, chat_auth_mode="api_key", chat_api_keys=SENTINEL_CHAT,
                cors_allowed_origins=SENTINEL_ORIGINS, trust_proxy_headers=True, trusted_proxies=SENTINEL_PROXIES)
    base.update(overrides)
    return dev_settings(**base)


def every_sentinel() -> list[str]:
    return [*SENTINEL_SECRETS.values(), *SENTINEL_CHAT.values(), *SENTINEL_CHAT.keys(), *SENTINEL_INFRA.values(),
            *SENTINEL_ORIGINS, *SENTINEL_PROXIES]


def fetch(s: Settings):
    with TestClient(create_app(s), raise_server_exceptions=False) as c:
        return c.get(PATH, headers=auth(s) if s.observability_api_token else {})


# =========================================================================== #
# 1-2. Authentication (same dependency as the other observability endpoints)
# =========================================================================== #
def test_requires_observability_token_when_configured():
    s = dev_settings(observability_api_token=strong())
    token = s.observability_api_token.get_secret_value()
    with TestClient(create_app(s)) as c:
        missing = c.get(PATH)
        wrong = c.get(PATH, headers={"Authorization": "Bearer nope"})
        basic = c.get(PATH, headers={"Authorization": f"Basic {token}"})
        in_query = c.get(f"{PATH}?token={token}")
        ok = c.get(PATH, headers=auth(s))
    for r in (missing, wrong, basic, in_query):
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"
        assert r.headers["WWW-Authenticate"] == "Bearer"
        assert token not in r.text
    assert ok.status_code == 200
    assert token not in ok.text


def test_admin_token_or_chat_key_is_not_accepted_instead_of_observability_token():
    s = sentinel_settings()
    with TestClient(create_app(s)) as c:
        admin = c.get(PATH, headers={"Authorization": f"Bearer {SENTINEL_SECRETS['admin_api_token']}"})
        chat = c.get(PATH, headers={"Authorization": f"Bearer {SENTINEL_CHAT['sentinel-client-alpha']}"})
    assert admin.status_code == chat.status_code == 401


def test_open_in_development_without_token_like_other_observability_endpoints():
    r = fetch(dev_settings())
    assert r.status_code == 200
    assert r.json()["auth"]["observability_token_required"] is False


def test_protected_in_production():
    s = prod_settings()
    with TestClient(create_app(s)) as c:
        missing = c.get(PATH)
        ok = c.get(PATH, headers=auth(s))
    assert missing.status_code == 401
    assert ok.status_code == 200
    body = ok.json()
    assert body["app"]["environment"] == "production"
    assert body["http"]["docs_enabled"] is False
    assert body["production_checks"]["enforced"] is True
    # A production app only starts without errors, so only warnings can remain.
    assert {i["severity"] for i in body["production_checks"]["issues"]} <= {"warning"}


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_read_only_no_write_methods(method):
    s = dev_settings(observability_api_token=strong())
    with TestClient(create_app(s)) as c:
        r = getattr(c, method)(PATH, headers=auth(s))
    assert r.status_code == 405


# =========================================================================== #
# 3. Explicit allow-list
# =========================================================================== #
def test_response_matches_the_explicit_allow_list_exactly():
    body = fetch(sentinel_settings()).json()
    assert set(body) == set(ALLOWED) | {"providers", "production_checks"}
    for section, fields in ALLOWED.items():
        assert set(body[section]) == fields, section
    assert body["providers"] and all(set(p) == PROVIDER_FIELDS for p in body["providers"])
    assert set(body["production_checks"]) == {"enforced", "issues"}
    assert all(set(i) == ISSUE_FIELDS for i in body["production_checks"]["issues"])


def _all_keys(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _all_keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _all_keys(v)}
    return set()


def test_settings_object_is_never_dumped_wholesale():
    # Checks FIELD NAMES (fixed messages may legitimately mention e.g. the auth mode 'api_key').
    keys = _all_keys(fetch(sentinel_settings()).json())
    keys -= {"model_limits"}  # its keys are model ids, checked by the allow-list test
    def has_words(key: str, fragment: str) -> bool:  # whole underscore-delimited words ("port" != "supported")
        return f"_{fragment}_" in f"_{key}_"

    for fragment in ("api_key", "api_token", "password", "hash_key", "host", "port", "user", "otlp",
                     "endpoint", "chat_api_keys", "allowed_origins", "trusted_proxies", "registry_file", "url",
                     "secret", "token"):
        offending = [k for k in keys if has_words(k, fragment)
                     and k not in {"trusted_proxies_configured", "observability_token_required"}]
        assert not offending, (fragment, offending)


# =========================================================================== #
# 4-5. No secret values: unique dummy secrets in isolated settings
# =========================================================================== #
def test_no_configured_secret_or_infrastructure_value_appears_in_response(caplog):
    s = sentinel_settings()
    # Sanity: the sentinels really are the configured secrets the guard and redaction know about.
    assert set(SENTINEL_SECRETS.values()) | set(SENTINEL_CHAT.values()) <= set(s.secret_values())
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(s)) as c:
        r = c.get(PATH, headers=auth(s))
    assert r.status_code == 200
    exposed = r.text + "\n".join(f"{k}: {v}" for k, v in r.headers.items())
    for value in every_sentinel():
        assert value not in exposed, "a configured secret/infrastructure value leaked into the response"
        assert value not in caplog.text, "a configured secret/infrastructure value leaked into the logs"
    # Nothing derived from secrets: no prefixes, no lengths.
    for value in (*SENTINEL_SECRETS.values(), *SENTINEL_CHAT.values()):
        assert value[:8] not in r.text
    body = r.json()
    assert body["auth"]["chat_clients_configured"] == 2
    assert body["http"]["cors_origins_configured"] == 2
    assert body["rate_limits"]["trusted_proxies_configured"] == 1


def test_leak_guard_refuses_the_view_if_a_secret_would_be_returned(caplog):
    token = strong()
    # A (mis)configuration that would echo a secret through an allowed field: app name == admin token.
    s = dev_settings(admin_api_token=token, app_name=token)
    with pytest.raises(SettingsLeakError) as exc:
        build_settings_view(s, load_registry(None), uptime_seconds=1.0)
    assert token not in str(exc.value)
    with TestClient(create_app(s), raise_server_exceptions=False) as c:
        r = c.get(PATH)
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    assert token not in r.text
    assert "settings_view.secret_detected" in caplog.text


# =========================================================================== #
# 6. Provider credential status: configured / not configured only
# =========================================================================== #
def test_provider_credential_status_is_boolean_only():
    s = dev_settings(openai_api_key="sk-SENTINEL-only-openai-xxxxxxxx")
    providers = {p["provider"]: p for p in fetch(s).json()["providers"]}
    assert providers["openai"]["credential_configured"] is True
    assert providers["openai"]["credential_setting"] == "OPENAI_API_KEY"
    for name in ("anthropic", "gemini", "groq", "mistral"):
        assert providers[name]["credential_required"] is True
        assert providers[name]["credential_configured"] is False
    # Providers that need no credential are reported as such (null), never as "configured".
    for name in ("ollama", "mock"):
        assert providers[name]["credential_required"] is False
        assert providers[name]["credential_configured"] is None
        assert providers[name]["credential_setting"] is None
    assert providers["mock"]["supported_for_real_calls"] is False
    assert providers["mock"]["models"]  # the built-in mock registry
    for p in providers.values():
        assert p["credential_configured"] in (True, False, None)


def test_provider_models_come_from_the_registry():
    s = dev_settings()
    body = fetch(s).json()
    registry = load_registry(None)
    listed = sorted(m for p in body["providers"] for m in p["models"])
    assert listed == sorted(m.id for m in registry.all())


# =========================================================================== #
# 7. Optional / unset configuration is represented accurately
# =========================================================================== #
def test_unset_limits_are_null_not_zero():
    body = fetch(dev_settings(rate_limit_per_minute=0, rate_limit_per_hour=0)).json()
    budget = body["budget"]
    assert budget["configured"] is False
    assert budget["daily_usd"] is None and budget["daily_usd_per_client"] is None
    assert budget["daily_tokens_per_client"] is None and budget["max_cost_per_request_usd"] is None
    assert budget["monthly_limit_supported"] is False
    assert body["rate_limits"]["per_minute"] is None and body["rate_limits"]["per_hour"] is None
    assert body["http"]["hsts_enabled"] is False and body["http"]["hsts_max_age_seconds"] is None
    assert body["http"]["cors_enabled"] is False and body["http"]["cors_origins_configured"] == 0
    assert body["cache"]["embedding_provider"] == "local" and body["cache"]["embedding_model"] is None
    assert body["auth"]["admin_endpoints_enabled"] is False
    assert body["auth"]["chat_auth_mode"] == "none" and body["auth"]["chat_clients_configured"] == 0
    assert body["auth"]["client_id_hashing"] == "sha256"
    assert body["routing"]["model_registry"] == "built_in"


def test_configured_values_are_reported():
    s = dev_settings(budget_daily_usd=5.0, budget_daily_usd_per_client=1.5, budget_daily_tokens_per_client=20000,
                     budget_max_cost_per_request_usd=0.25, budget_fail_closed=True, hsts_enabled=True,
                     rate_limit_per_minute=30, rate_limit_per_hour=500, model_rate_limits={"mock-advanced": 7},
                     admin_api_token=strong(), client_id_hash_key=strong())
    body = fetch(s).json()
    assert body["budget"]["configured"] is True
    assert (body["budget"]["daily_usd"], body["budget"]["daily_usd_per_client"]) == (5.0, 1.5)
    assert body["budget"]["daily_tokens_per_client"] == 20000
    assert body["budget"]["max_cost_per_request_usd"] == 0.25
    assert body["http"]["hsts_max_age_seconds"] == s.hsts_max_age_seconds
    assert (body["rate_limits"]["per_minute"], body["rate_limits"]["per_hour"]) == (30, 500)
    assert body["rate_limits"]["model_limits"] == {"mock-advanced": 7}
    assert body["auth"]["admin_endpoints_enabled"] is True
    assert body["auth"]["client_id_hashing"] == "hmac"
    assert body["mode"] == {"mock_mode": True, "allow_real_provider_calls": False,
                            "real_provider_calls_enabled": False}
    assert body["app"]["environment"] == "test"
    assert body["app"]["uptime_seconds"] >= 0


# =========================================================================== #
# 8. Production warnings: setting names and safe messages only
# =========================================================================== #
def test_production_issues_are_sanitised():
    # Development defaults include the KNOWN default database password; chat keys are short
    # and the client name is distinctive. None of it may surface through the checks.
    s = dev_settings(database_enabled=True, chat_auth_mode="api_key",
                     chat_api_keys={"sentinel-client-gamma": "SENTINEL-short-key-0013"})
    view = build_settings_view(s, load_registry(None), uptime_seconds=0.0)
    issues = view.production_checks.issues
    text = view.model_dump_json()
    assert view.production_checks.enforced is False  # test environment: informational only
    assert "router_password" not in text  # the development default password
    assert "sentinel-client-gamma" not in text and "SENTINEL-short-key-0013" not in text
    settings = {i.setting for i in issues}
    assert "CHAT_API_KEYS" in settings and "POSTGRES_PASSWORD" in settings
    assert not any("[" in name for name in settings)
    for issue in issues:
        if issue.setting in {"CHAT_API_KEYS", "POSTGRES_PASSWORD", "ADMIN_API_TOKEN", "OBSERVABILITY_API_TOKEN",
                             "CLIENT_ID_HASH_KEY", "REDIS_PASSWORD"}:
            assert issue.message == SECRET_ISSUE_MESSAGE  # no lengths, no "default password" hint
        assert "characters" not in issue.message
    # Non-secret warnings keep their original, fixed messages.
    by_setting = {i.setting: i for i in issues}
    assert by_setting["HSTS_ENABLED"].severity == "warning"
    assert by_setting["HSTS_ENABLED"].message == "enable once the API is served over HTTPS"
    assert by_setting["LLM_MOCK_MODE"].severity == "warning"


def test_several_chat_clients_collapse_into_one_issue():
    s = dev_settings(chat_auth_mode="api_key", chat_api_keys={"a-client": "SENTINEL-short-a-0014",
                                                              "b-client": "SENTINEL-short-b-0015"})
    issues = build_settings_view(s, load_registry(None), uptime_seconds=0.0).production_checks.issues
    assert [i.setting for i in issues].count("CHAT_API_KEYS") == 1


# =========================================================================== #
# 9-10. Headers: no-store and the existing security headers
# =========================================================================== #
def test_no_store_and_security_headers():
    r = fetch(dev_settings(observability_api_token=strong()))
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Content-Security-Policy"].startswith("default-src 'none'")


def test_no_store_even_when_security_headers_are_disabled():
    r = fetch(dev_settings(security_headers_enabled=False))
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    assert r.json()["http"]["security_headers_enabled"] is False


def test_listed_in_openapi_as_get_only():
    with TestClient(create_app(dev_settings())) as c:
        paths = c.get("/openapi.json").json()["paths"]
    assert set(paths[PATH]) == {"get"}
