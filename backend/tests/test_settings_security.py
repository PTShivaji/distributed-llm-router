"""Phase 5 step 1: test isolation from the developer's .env and secret-safe Settings."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core import config
from app.core.config import Settings

BACKEND = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Test isolation
# --------------------------------------------------------------------------- #
def test_test_suite_never_loads_project_env_file():
    assert config.ENV_FILE_DISABLED is True
    assert config.ENV_FILE is None
    assert Settings.model_config["env_file"] is None


def test_env_file_is_loaded_when_not_disabled():
    """Outside tests the root .env is still the configured source (behaviour unchanged).

    Runs in a subprocess WITHOUT LLMR_DISABLE_ENV_FILE and only inspects the
    configured path - it never reads the file's contents.
    """
    env = {k: v for k, v in __import__("os").environ.items() if k != "LLMR_DISABLE_ENV_FILE"}
    code = "from app.core import config; print(config.ENV_FILE)"
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, check=True)
    assert Path(out.stdout.strip()) == config.PROJECT_ROOT / ".env"


def test_get_settings_ignores_env_file(monkeypatch):
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().model_config["env_file"] is None
    finally:
        config.get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Secret handling
# --------------------------------------------------------------------------- #
SECRETS = {
    "postgres_password": "pg-Secret-1!",
    "redis_password": "redis-Secret-2!",
    "openai_api_key": "sk-openai-secret-3",
    "admin_api_token": "admin-secret-4",
}


@pytest.fixture
def secret_settings() -> Settings:
    return Settings(_env_file=None, **SECRETS)


def test_passwords_are_secretstr(secret_settings):
    assert isinstance(secret_settings.postgres_password, SecretStr)
    assert isinstance(secret_settings.redis_password, SecretStr)


@pytest.mark.parametrize(
    "render",
    [
        repr,
        str,
        lambda s: s.model_dump_json(),
        lambda s: json.dumps(s.model_dump(mode="json")),
        lambda s: str(s.model_dump()),
    ],
    ids=["repr", "str", "model_dump_json", "model_dump_json_mode", "model_dump_python"],
)
def test_no_secret_in_any_rendering(secret_settings, render):
    text = render(secret_settings)
    for value in SECRETS.values():
        assert value not in text


def test_connection_urls_not_in_dumps(secret_settings):
    dumped = secret_settings.model_dump()
    assert "database_url" not in dumped and "redis_url" not in dumped


def test_connection_urls_still_work_and_are_url_encoded(secret_settings):
    # Real URLs keep working for the client libraries (with special chars escaped).
    assert secret_settings.database_url == "postgresql+psycopg://router:pg-Secret-1%21@localhost:5433/llm_router"
    assert secret_settings.redis_url == "redis://:redis-Secret-2%21@localhost:6379/0"


def test_safe_urls_mask_passwords(secret_settings):
    assert secret_settings.safe_database_url == "postgresql+psycopg://router:***@localhost:5433/llm_router"
    assert secret_settings.safe_redis_url == "redis://:***@localhost:6379/0"
    assert Settings(_env_file=None).safe_redis_url == "redis://localhost:6379/0"


def test_password_with_url_reserved_characters_round_trips():
    from sqlalchemy.engine import make_url

    s = Settings(_env_file=None, postgres_password="p@ss:w/rd#?")
    assert make_url(s.database_url).password == "p@ss:w/rd#?"


def test_blank_redis_password_is_none():
    assert Settings(_env_file=None, redis_password="  ").redis_password is None


def test_env_var_passwords_become_secretstr(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-env-pw")
    s = Settings(_env_file=None)
    assert s.postgres_password.get_secret_value() == "from-env-pw"
    assert "from-env-pw" not in repr(s)
