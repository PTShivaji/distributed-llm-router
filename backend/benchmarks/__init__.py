"""Reproducible performance benchmarks for the Distributed Multi-Model Router.

Run from backend/:   python -m benchmarks.run --help
All scenarios use the deterministic mock provider; no paid API calls are made.
Redis/PostgreSQL scenarios use isolated namespaces (Redis DB 15 + a unique
"bench:<id>:" key prefix; database llm_router_test) and clean up afterwards.
"""

import os

# Benchmarks must be reproducible: never read the developer's project-root .env.
# (Set before any `app` module is imported; see app/core/config.py.)
os.environ.setdefault("LLMR_DISABLE_ENV_FILE", "1")
# Never let `import litellm` load a .env or fetch its cost map (app/providers/litellm_env.py).
os.environ["LITELLM_MODE"] = "PRODUCTION"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
