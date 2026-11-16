"""The ONLY place `litellm` is imported (lazily, it is slow).

`import litellm` has two side effects this project must never allow:
  * with LITELLM_MODE unset or "DEV" it calls python-dotenv `load_dotenv()`,
    which searches upward from the installed package and loads the first `.env`
    it finds - the project-root `.env` - into os.environ. That would silently
    pull secrets into the process and break test isolation.
  * unless LITELLM_LOCAL_MODEL_COST_MAP=True it downloads a model cost map from
    GitHub at import time (a network call; this app uses its own registry prices).

Defences (all applied on every import through `import_litellm`):
  1. LITELLM_MODE=PRODUCTION and LITELLM_LOCAL_MODEL_COST_MAP=True are FORCED
     (not defaults): a stray LITELLM_MODE=DEV in the shell cannot re-enable it.
  2. PYTHON_DOTENV_DISABLED=1 is set for the duration of the import, so even a
     future LiteLLM code path that calls load_dotenv() is a no-op. The previous
     value is restored afterwards. This does NOT affect the app's own settings:
     pydantic-settings reads ENV_FILE with dotenv_values(), which ignores the flag.
  3. After the import the set of environment variable NAMES is compared; any
     unexpected addition is reported (count only, never names or values).

The application's configuration still loads exactly as before: Settings
(app/core/config.py) reads the project-root .env via pydantic-settings unless
LLMR_DISABLE_ENV_FILE=1 (tests, benchmarks).
"""

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType

logger = logging.getLogger("app.providers.litellm_env")

FORCED_LITELLM_ENV = {"LITELLM_MODE": "PRODUCTION", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
DOTENV_DISABLE_VAR = "PYTHON_DOTENV_DISABLED"


class LiteLLMEnvironmentError(RuntimeError):
    """litellm was imported before the safe environment could be applied."""


def apply_safe_litellm_env() -> None:
    os.environ.update(FORCED_LITELLM_ENV)


@contextmanager
def dotenv_loading_disabled() -> Iterator[None]:
    previous = os.environ.get(DOTENV_DISABLE_VAR)
    os.environ[DOTENV_DISABLE_VAR] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(DOTENV_DISABLE_VAR, None)
        else:
            os.environ[DOTENV_DISABLE_VAR] = previous


def import_litellm() -> ModuleType:
    already = sys.modules.get("litellm")
    if already is not None:
        if os.environ.get("LITELLM_MODE") != "PRODUCTION":
            # Someone imported litellm directly (bypassing this module) without the guard.
            raise LiteLLMEnvironmentError("litellm was imported without app.providers.litellm_env")
        return already

    apply_safe_litellm_env()
    before = set(os.environ)
    with dotenv_loading_disabled():
        import litellm  # noqa: PLC0415 - intentionally lazy (slow import)
    added = set(os.environ) - before
    if added:
        logger.error("litellm.import_changed_environment added_variables=%d", len(added))
    litellm.suppress_debug_info = True
    return litellm
