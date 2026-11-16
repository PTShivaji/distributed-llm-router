"""GET /api/v1/settings: read-only, non-secret view of the effective configuration.

Protected exactly like the other observability endpoints (require_observability_token):
OBSERVABILITY_API_TOKEN set -> "Authorization: Bearer <token>" required (production refuses
to start without it); unset (development only) -> open.

There is deliberately no write endpoint: configuration changes go through environment
variables and a restart. See app/core/settings_view.py for the allow-list and leak guard.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.deps import get_container, require_observability_token
from app.api.health import process_uptime_seconds
from app.core.settings_view import build_settings_view
from app.infra.container import Container
from app.schemas.settings import SettingsResponse

router = APIRouter(tags=["settings"])


@router.get("/settings", response_model=SettingsResponse,
            summary="Effective configuration (non-secret allow-list; no values of keys, tokens or passwords)",
            dependencies=[Depends(require_observability_token)])
async def get_settings_view(container: Annotated[Container, Depends(get_container)],
                            response: Response) -> SettingsResponse:
    # Explicit, independent of SECURITY_HEADERS_ENABLED (which also adds no-store to /api/*).
    response.headers["Cache-Control"] = "no-store"
    return build_settings_view(container.settings, container.chat_service.policy.registry,
                               uptime_seconds=process_uptime_seconds())
