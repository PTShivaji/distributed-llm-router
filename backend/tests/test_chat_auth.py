"""Phase 5 step 8: configurable authentication for POST /api/v1/chat."""

import logging
import secrets

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.production import check_production_settings
from app.main import create_app
from app.routing.service import ChatService

CHAT = "/api/v1/chat"
Q = {"query": "What is the capital of France?"}


def key() -> str:
    return secrets.token_urlsafe(32)


KEY_A, KEY_B = key(), key()


def settings(**overrides) -> Settings:
    base = dict(_env_file=None, environment="test", llm_mock_mode=True, mock_failures={}, log_level="WARNING",
                redis_enabled=False, database_enabled=False, cache_enabled=False, rate_limit_enabled=False,
                health_backend="memory")
    base.update(overrides)
    return Settings(**base)


def auth_settings(**overrides) -> Settings:
    return settings(chat_auth_mode="api_key", chat_api_keys={"team-a": KEY_A, "team-b": KEY_B}, **overrides)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Defaults: development keeps working without credentials (contract unchanged)
# --------------------------------------------------------------------------- #
def test_default_mode_is_none_and_chat_needs_no_credentials():
    s = Settings(_env_file=None)
    assert s.chat_auth_mode == "none" and s.chat_api_keys == {}
    with TestClient(create_app(settings())) as c:
        r = c.post(CHAT, json=Q)
    assert r.status_code == 200 and r.json()["mock_mode"] is True


def test_api_key_mode_accepts_each_configured_key():
    with TestClient(create_app(auth_settings())) as c:
        a = c.post(CHAT, json=Q, headers=bearer(KEY_A))
        b = c.post(CHAT, json=Q, headers={"authorization": f"bearer {KEY_B}"})  # scheme is case-insensitive
    assert a.status_code == b.status_code == 200


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": ""},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer "},
    {"Authorization": "Bearer wrong-key-wrong-key-wrong"},
    {"Authorization": f"Basic {KEY_A}"},
    {"Authorization": f"Token {KEY_A}"},
    {"X-API-Key": KEY_A},
    {"Authorization": f"Bearer {KEY_A[:-1]}"},  # prefix of a real key
    {"Authorization": f"Bearer {KEY_A}x"},  # real key + suffix
])
def test_missing_or_invalid_credentials_are_401(headers):
    with TestClient(create_app(auth_settings())) as c:
        r = c.post(CHAT, json=Q, headers=headers)
    assert r.status_code == 401
    body = r.json()["error"]
    assert body["code"] == "unauthorized" and r.headers["X-Request-ID"]
    assert r.headers["WWW-Authenticate"] == 'Bearer realm="chat"'
    assert KEY_A not in r.text and KEY_B not in r.text


def test_key_in_query_or_body_is_not_accepted():
    with TestClient(create_app(auth_settings())) as c:
        in_query = c.post(f"{CHAT}?api_key={KEY_A}", json=Q)
        in_body = c.post(CHAT, json={**Q, "api_key": KEY_A})
    assert in_query.status_code == 401
    assert in_body.status_code in (401, 422)  # never authenticated by a body field
    assert KEY_A not in in_body.text


def test_admin_and_observability_tokens_do_not_authenticate_chat_and_vice_versa():
    admin, obs = key(), key()
    with TestClient(create_app(auth_settings(admin_api_token=admin, observability_api_token=obs))) as c:
        assert c.post(CHAT, json=Q, headers=bearer(admin)).status_code == 401
        assert c.post(CHAT, json=Q, headers=bearer(obs)).status_code == 401
        assert c.get("/metrics", headers=bearer(KEY_A)).status_code == 401
        assert c.delete("/api/v1/cache", headers=bearer(KEY_A)).status_code == 401
        assert c.get("/metrics", headers=bearer(obs)).status_code == 200


def test_other_endpoints_are_unaffected_by_chat_auth():
    with TestClient(create_app(auth_settings())) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/api/v1/models").status_code == 200
        assert c.post("/api/v1/classify", json=Q).status_code == 200


def test_validation_errors_still_require_auth_first():
    with TestClient(create_app(auth_settings())) as c:
        unauth_bad = c.post(CHAT, json={})
        auth_bad = c.post(CHAT, json={}, headers=bearer(KEY_A))
    # FastAPI validates the body before dependencies run; neither leaks anything.
    assert unauth_bad.status_code in (401, 422) and auth_bad.status_code == 422


# --------------------------------------------------------------------------- #
# Identity: the CLIENT NAME (never the key) flows into rate limits / records
# --------------------------------------------------------------------------- #
def test_authenticated_client_name_is_the_client_identity(monkeypatch):
    seen: list[str | None] = []
    original = ChatService.chat

    async def spy(self, cmd):
        seen.append(cmd.client_id)
        return await original(self, cmd)

    monkeypatch.setattr(ChatService, "chat", spy)
    with TestClient(create_app(auth_settings())) as c:
        c.post(CHAT, json=Q, headers=bearer(KEY_A))
        c.post(CHAT, json=Q, headers=bearer(KEY_B))
    with TestClient(create_app(settings())) as c:
        c.post(CHAT, json=Q)
    assert seen == ["chat-key:team-a", "chat-key:team-b", "testclient"]
    assert all(KEY_A not in (s or "") and KEY_B not in (s or "") for s in seen)


def test_rate_limits_apply_per_api_key_and_401_consumes_no_quota(infra_settings):
    s = infra_settings(chat_auth_mode="api_key", chat_api_keys={"team-a": KEY_A, "team-b": KEY_B},
                       rate_limit_per_minute=1, rate_limit_per_hour=0, cache_enabled=False)
    with TestClient(create_app(s)) as c:
        bad = [c.post(CHAT, json=Q, headers=bearer("nope-nope-nope-nope")) for _ in range(3)]
        a1 = c.post(CHAT, json=Q, headers=bearer(KEY_A))
        a2 = c.post(CHAT, json=Q, headers=bearer(KEY_A))
        b1 = c.post(CHAT, json=Q, headers=bearer(KEY_B))  # same IP, different key -> own quota
    assert [r.status_code for r in bad] == [401, 401, 401]
    assert (a1.status_code, a2.status_code, b1.status_code) == (200, 429, 200)


# --------------------------------------------------------------------------- #
# Logging / redaction
# --------------------------------------------------------------------------- #
def test_keys_never_appear_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(auth_settings(log_level="DEBUG"))) as c:
        c.post(CHAT, json=Q, headers=bearer(KEY_A))
        c.post(CHAT, json=Q, headers=bearer(KEY_A + "-tampered"))
        logging.getLogger("app.test").warning("echo %s", KEY_B)  # even if code logs one by mistake
    assert KEY_A not in caplog.text and KEY_B not in caplog.text
    assert "chat.unauthorized" in caplog.text and "invalid_credentials" in caplog.text


def test_chat_keys_are_registered_for_redaction_and_masked_in_dumps():
    s = auth_settings()
    assert KEY_A in s.secret_values() and KEY_B in s.secret_values()
    dumped = repr(s) + str(s.model_dump()) + s.model_dump_json()
    assert KEY_A not in dumped and KEY_B not in dumped


# --------------------------------------------------------------------------- #
# Configuration validation (every environment) - messages never echo keys
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("overrides, fragment", [
    ({"chat_auth_mode": "api_key"}, "requires at least one entry"),
    ({"chat_api_keys": {"a": "short"}}, "at least 16 characters"),
    ({"chat_api_keys": {"bad name!": KEY_A}}, "client names"),
    ({"chat_api_keys": {"a": KEY_A, "b": KEY_A}}, "distinct key"),
    ({"chat_api_keys": {"a": KEY_A}, "admin_api_token": KEY_A}, "must not reuse"),
    ({"chat_api_keys": {"a": KEY_A}, "observability_api_token": KEY_A}, "must not reuse"),
])
def test_invalid_chat_auth_configuration_is_rejected_without_echoing_keys(overrides, fragment):
    with pytest.raises(ValidationError) as exc:
        settings(**overrides)
    # What gets printed/logged (str) and the input-free error list never contain the key.
    # NOTE: pydantic still exposes raw input via exc.errors() (default include_input=True);
    # nothing in the app logs that for Settings.
    text = str(exc.value) + repr(exc.value.errors(include_input=False))
    assert fragment in text and KEY_A not in text


def test_settings_validation_errors_never_echo_input_values():
    secret = key()
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, postgres_password=secret, port="not-a-port")
    assert secret not in str(exc.value) and "not-a-port" not in str(exc.value)


def test_chat_keys_load_from_environment_json(monkeypatch):
    monkeypatch.setenv("CHAT_AUTH_MODE", "api_key")
    monkeypatch.setenv("CHAT_API_KEYS", f'{{"svc-1": "{KEY_A}"}}')
    s = Settings(_env_file=None)
    assert s.chat_auth_mode == "api_key" and s.chat_api_keys["svc-1"].get_secret_value() == KEY_A


def test_production_requires_chat_auth_with_strong_distinct_keys():
    base = dict(environment="production")
    issues = {i.setting for i in check_production_settings(settings(**base)) if i.severity == "error"}
    assert "CHAT_AUTH_MODE" in issues
    weak = settings(**base, chat_auth_mode="api_key", chat_api_keys={"a": "only-sixteen-ch!"})
    assert "CHAT_API_KEYS[a]" in {i.setting for i in check_production_settings(weak) if i.severity == "error"}
    same = key()
    shared = settings(**base, admin_api_token=same, observability_api_token=same)
    assert "OBSERVABILITY_API_TOKEN" in {i.setting for i in check_production_settings(shared) if i.severity == "error"}
    text = " ".join(str(i) for i in check_production_settings(weak))
    assert "only-sixteen-ch!" not in text
