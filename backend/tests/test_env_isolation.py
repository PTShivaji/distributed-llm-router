"""Step 8 audit: LiteLLM / python-dotenv can never load the project-root .env.

SAFETY: every subprocess below replaces python-dotenv's `load_dotenv` and
`find_dotenv` with RECORDERS before anything else is imported. The recorders
never touch the filesystem, so even if the guard under test were broken, no
real .env file could be read - the test would fail instead. Only counts and
booleans are printed; no variable names or values from any environment.
"""

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import ENV_FILE, PROJECT_ROOT, Settings
from app.providers import litellm_env

BACKEND = PROJECT_ROOT / "backend"

RECORDER = """
import json, os, sys
import dotenv, dotenv.main
calls = []
def _rec_load(*a, **k):
    calls.append({"fn": "load_dotenv", "disabled": dotenv.main._load_dotenv_disabled()})
    return False
def _rec_find(*a, **k):
    calls.append({"fn": "find_dotenv"})
    return ""
dotenv.load_dotenv = dotenv.main.load_dotenv = _rec_load
dotenv.find_dotenv = dotenv.main.find_dotenv = _rec_find
before = set(os.environ)
"""


def run_isolated(body: str, **extra_env: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("LITELLM_") and k != "PYTHON_DOTENV_DISABLED"}
    env.update(LLMR_DISABLE_ENV_FILE="1", **extra_env)
    code = RECORDER + body + "\nprint('RESULT=' + json.dumps(result))\n"
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True,
                         timeout=300)
    assert out.returncode == 0, "isolated subprocess failed (stderr suppressed: may contain environment data)"
    line = next(l for l in out.stdout.splitlines() if l.startswith("RESULT="))
    return json.loads(line[len("RESULT="):])


# --------------------------------------------------------------------------- #
# Layer 1: LITELLM_MODE is forced, even over a stray LITELLM_MODE=DEV
# --------------------------------------------------------------------------- #
def test_guarded_import_never_calls_load_dotenv_even_with_stray_dev_mode():
    result = run_isolated("""
from app.providers.litellm_env import import_litellm
import_litellm()
added = set(os.environ) - before - {"LITELLM_MODE", "LITELLM_LOCAL_MODEL_COST_MAP"}
result = {"calls": calls, "mode": os.environ.get("LITELLM_MODE"),
          "cost_map": os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP"),
          "flag_left": "PYTHON_DOTENV_DISABLED" in os.environ, "added": len(added)}
""", LITELLM_MODE="DEV", LITELLM_LOCAL_MODEL_COST_MAP="False")
    assert result == {"calls": [], "mode": "PRODUCTION", "cost_map": "True", "flag_left": False, "added": 0}


# --------------------------------------------------------------------------- #
# Layer 2: with layer 1 removed, LiteLLM's load_dotenv() runs DISABLED
# --------------------------------------------------------------------------- #
def test_dotenv_is_disabled_during_import_even_if_litellm_mode_guard_is_bypassed():
    result = run_isolated("""
import app.providers.litellm_env as le
le.apply_safe_litellm_env = lambda: None      # simulate layer 1 failing
le.import_litellm()
result = {"calls": calls, "mode": os.environ.get("LITELLM_MODE"),
          "flag_left": "PYTHON_DOTENV_DISABLED" in os.environ}
""", LITELLM_MODE="DEV", LITELLM_LOCAL_MODEL_COST_MAP="True")
    # LiteLLM did try (proves the hazard is real) - but python-dotenv was disabled at that moment.
    assert result["mode"] == "DEV"
    assert [c for c in result["calls"] if c["fn"] == "load_dotenv"] == [{"fn": "load_dotenv", "disabled": True}]
    assert result["flag_left"] is False  # restored after the import


def test_a_direct_unguarded_litellm_import_is_detected():
    result = run_isolated("""
os.environ["PYTHON_DOTENV_DISABLED"] = "1"     # keep this probe itself safe
import litellm                                  # bypasses app.providers.litellm_env
from app.providers.litellm_env import LiteLLMEnvironmentError, import_litellm
try:
    import_litellm()
    result = {"raised": False}
except LiteLLMEnvironmentError:
    result = {"raised": True}
""", LITELLM_MODE="DEV", LITELLM_LOCAL_MODEL_COST_MAP="True")
    assert result == {"raised": True}


# --------------------------------------------------------------------------- #
# In-process checks (no LiteLLM import needed)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("previous", [None, "0"])
def test_dotenv_disable_context_blocks_load_and_restores_previous_value(monkeypatch, previous):
    import dotenv

    if previous is None:
        monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    else:
        monkeypatch.setenv("PYTHON_DOTENV_DISABLED", previous)
    monkeypatch.delenv("STEP8_DOTENV_SENTINEL", raising=False)
    with litellm_env.dotenv_loading_disabled():
        loaded = dotenv.load_dotenv(stream=io.StringIO("STEP8_DOTENV_SENTINEL=leaked\n"))
    assert loaded is False and "STEP8_DOTENV_SENTINEL" not in os.environ
    assert os.environ.get("PYTHON_DOTENV_DISABLED") == previous


def test_app_settings_still_load_from_an_explicit_env_file_while_dotenv_is_disabled(monkeypatch, tmp_path):
    """The intended mechanism (pydantic-settings env_file) is unaffected by the kill switch."""
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.delenv("APP_NAME", raising=False)
    env_file = tmp_path / "custom.env"
    env_file.write_text("APP_NAME=Loaded From File\nCHAT_AUTH_MODE=none\n", encoding="utf-8")
    assert Settings(_env_file=env_file).app_name == "Loaded From File"
    assert "APP_NAME" not in os.environ  # read into Settings only, never exported to os.environ


def test_the_test_process_never_reads_the_root_env():
    assert ENV_FILE is None  # LLMR_DISABLE_ENV_FILE=1 set by conftest before app import
    assert Settings.model_config["env_file"] is None
    assert os.environ["LITELLM_MODE"] == "PRODUCTION"
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"


def test_benchmarks_package_applies_the_same_isolation():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LITELLM_", "LLMR_")) and k != "PYTHON_DOTENV_DISABLED"}
    code = ("import os, benchmarks, json; from app.core.config import ENV_FILE; print(json.dumps("
            "[ENV_FILE is None, os.environ['LITELLM_MODE'], os.environ['PYTHON_DOTENV_DISABLED']]))")
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [True, "PRODUCTION", "1"]


def test_litellm_is_imported_only_through_the_guard_module():
    pattern = re.compile(r"^\s*(import litellm|from litellm\b)", re.MULTILINE)
    offenders = [
        str(p.relative_to(BACKEND)) for root in ("app", "benchmarks") for p in (BACKEND / root).rglob("*.py")
        if p.name != "litellm_env.py" and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
