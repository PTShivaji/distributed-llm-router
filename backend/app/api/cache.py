"""Cache management endpoints. Never return cached prompts or answers."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import get_container, require_admin, require_observability_token
from app.core.errors import AppError
from app.infra.container import Container
from app.infra.redis import REDIS_ERRORS
from app.schemas.infra import CacheClearResponse, CacheStatsResponse

router = APIRouter(prefix="/cache", tags=["cache"])


class CacheUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "cache_unavailable"


@router.get("/stats", response_model=CacheStatsResponse, summary="Cache statistics (no cached content)",
            dependencies=[Depends(require_observability_token)])
async def cache_stats(container: Annotated[Container, Depends(get_container)]) -> CacheStatsResponse:
    cache = container.cache
    if cache is None:
        return CacheStatsResponse(
            enabled=False, semantic_enabled=False, available=False, embedding_provider=None,
            embedding_is_semantic=False, similarity_threshold=None, ttl_seconds=None, total_entries=None,
            scopes=None, hits=0, hits_exact=0, hits_semantic=0, misses=0, stores=0, hit_rate=None,
            approx_storage_bytes=None, notes=["Cache disabled (CACHE_ENABLED=false or REDIS_ENABLED=false)."],
        )
    stats = await cache.stats()
    return CacheStatsResponse.model_validate(stats)


@router.delete(
    "",
    response_model=CacheClearResponse,
    dependencies=[Depends(require_admin)],
    summary="Clear the response cache (requires ADMIN_API_TOKEN bearer token)",
)
async def clear_cache(
    container: Annotated[Container, Depends(get_container)],
    reset_stats: Annotated[bool, Query(description="Also reset hit/miss counters.")] = False,
) -> CacheClearResponse:
    if container.cache is None:
        raise CacheUnavailableError("Cache is disabled.")
    try:
        deleted = await container.cache.clear(reset_stats=reset_stats)
    except REDIS_ERRORS as exc:
        raise CacheUnavailableError(f"Redis unavailable ({type(exc).__name__}).") from exc
    return CacheClearResponse(cleared=True, deleted_keys=deleted, reset_stats=reset_stats)
