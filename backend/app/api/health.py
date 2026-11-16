"""Health-check endpoints.

This is a *liveness* check: it confirms the API process is up and serving
requests. It deliberately does not depend on Redis/PostgreSQL so the app stays
healthy (and testable) without external services. Dependency readiness checks
will be added in a later phase.
"""

import time
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.deps import require_observability_token
from app.core.config import Settings, get_settings
from app.schemas.infra import DependenciesResponse, DependencyStatus

router = APIRouter(tags=["health"])

_STARTED_AT = time.monotonic()


def process_uptime_seconds() -> float:
    """Seconds since this API process started (shared by /health and /api/v1/settings)."""
    return round(time.monotonic() - _STARTED_AT, 3)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    app_name: str
    version: str
    environment: str
    timestamp: datetime
    uptime_seconds: float


@router.get("/health/dependencies", response_model=DependenciesResponse, summary="Redis / PostgreSQL reachability",
            dependencies=[Depends(require_observability_token)])
async def dependencies(request: Request) -> DependenciesResponse:
    """Readiness-style detail. Unlike /health, this touches Redis and PostgreSQL."""
    c = request.app.state.container
    recorder = c.recorder
    return DependenciesResponse(
        redis=DependencyStatus(enabled=c.redis.enabled, reachable=await c.redis.ping() if c.redis.started else False),
        database=DependencyStatus(enabled=c.database.enabled, reachable=await c.database.ping()),
        cache_enabled=c.cache is not None,
        rate_limit_enabled=c.rate_limiter is not None,
        health_backend=c.health.backend,
        persistence={
            "written": getattr(recorder, "written", 0),
            "dropped": getattr(recorder, "dropped", 0),
            "failed": getattr(recorder, "failed", 0),
        },
    )


@router.get("/health", response_model=HealthResponse, summary="Liveness check")
async def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    return HealthResponse(
        status="ok",
        app_name=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        timestamp=datetime.now(UTC),
        uptime_seconds=process_uptime_seconds(),
    )
