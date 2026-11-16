"""Environment description embedded in every result file (no secrets)."""

import importlib.metadata
import os
import platform
import sys
from datetime import UTC, datetime
from typing import Any

PACKAGES = ("fastapi", "starlette", "uvicorn", "httpx", "pydantic", "redis", "SQLAlchemy", "psycopg",
            "prometheus_client", "opentelemetry-sdk", "litellm")

# Only these settings are ever copied into results (all non-secret).
SAFE_SETTING_KEYS = (
    "app_version", "llm_mock_mode", "log_level", "access_log_enabled", "metrics_enabled", "tracing_enabled",
    "tracing_exporter", "tracing_sample_ratio", "redis_enabled", "cache_enabled", "cache_semantic_enabled", "cache_semantic_allow_lexical",
    "cache_similarity_threshold", "embedding_provider", "embedding_dimensions", "health_backend",
    "rate_limit_enabled", "database_enabled", "provider_max_retries", "routing_strategy",
    "cache_max_entries_per_scope",
)


def package_versions() -> dict[str, str]:
    out = {}
    for name in PACKAGES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            out[name] = "not installed"
    return out


def safe_settings(settings: Any) -> dict[str, Any]:
    return {k: getattr(settings, k) for k in SAFE_SETTING_KEYS if hasattr(settings, k)}


def environment_info() -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "python_version": sys.version.split()[0],
        "python_implementation": platform.python_implementation(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "cpu_count_logical": os.cpu_count(),
        "packages": package_versions(),
        "measurement_notes": [
            "in_process: httpx.ASGITransport calls the ASGI app directly (no sockets, no uvicorn); "
            "measures application overhead only.",
            "real_http: requests go over loopback TCP to separately spawned uvicorn processes; "
            "client and servers share this machine's CPUs.",
            "micro: single function calls in a tight loop.",
            "All providers are the deterministic MockProvider (no network, no paid APIs).",
        ],
    }
