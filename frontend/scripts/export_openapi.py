"""Export the backend's OpenAPI schema for frontend type generation (read-only).

    cd frontend
    npm run gen:openapi      # writes src/api/openapi.json
    npm run gen:types        # writes src/api/schema.gen.ts from it

Safety:
  * LLMR_DISABLE_ENV_FILE=1 is set BEFORE any backend import, so the project-root
    .env is never read (app.main builds a module-level app at import time).
  * The app is built from explicit settings: mock mode, Redis/PostgreSQL/cache/rate
    limiting disabled, no API keys. No connection is opened (the lifespan never runs)
    and LiteLLM is never imported.
  * No backend file is modified; only frontend/src/api/openapi.json is written.
"""

import json
import os
import pathlib
import sys

os.environ["LLMR_DISABLE_ENV_FILE"] = "1"
os.environ["LITELLM_MODE"] = "PRODUCTION"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

FRONTEND = pathlib.Path(__file__).resolve().parents[1]
BACKEND = FRONTEND.parent / "backend"
OUTPUT = FRONTEND / "src" / "api" / "openapi.json"
sys.path.insert(0, str(BACKEND))

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

settings = Settings(
    _env_file=None, environment="development", llm_mock_mode=True, allow_real_provider_calls=False,
    redis_enabled=False, database_enabled=False, cache_enabled=False, rate_limit_enabled=False,
    health_backend="memory", metrics_enabled=True, tracing_enabled=False, log_level="ERROR",
)
schema = create_app(settings).openapi()
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"wrote {OUTPUT.relative_to(FRONTEND)}: {len(schema['paths'])} paths, "
      f"{len(schema['components']['schemas'])} schemas (OpenAPI {schema['openapi']})")
