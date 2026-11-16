"""GET /metrics - Prometheus text exposition format.

Served at the root path (Prometheus convention), not under /api/v1.
Protected by OBSERVABILITY_API_TOKEN when that setting is configured.
"""

from fastapi import APIRouter, Depends, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.deps import get_container, require_observability_token

router = APIRouter(tags=["observability"])


@router.get(
    "/metrics",
    summary="Prometheus metrics (text exposition format)",
    dependencies=[Depends(require_observability_token)],
    response_class=Response,
    responses={200: {"content": {CONTENT_TYPE_LATEST: {}}, "description": "Prometheus text format"}},
)
async def metrics(request: Request) -> Response:
    registry = get_container(request).metrics.registry
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
