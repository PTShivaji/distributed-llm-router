"""FastAPI application entry point.

Run from the backend/ directory:
    uvicorn app.main:app --reload
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health
from app.api.router import api_v1_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.production import enforce_production_settings
from app.core.request_context import RequestIdMiddleware
from app.core.security import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from fastapi.middleware.cors import CORSMiddleware
from app.infra.container import build_container
from app.api import metrics as metrics_api
from app.observability.middleware import HTTPObservabilityMiddleware, TracingMiddleware
from app.ratelimit.limiter import configure_client_hashing
from app.routing.service import ChatService

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, chat_service: ChatService | None = None) -> FastAPI:
    """Application factory: builds a fully configured FastAPI instance.

    `chat_service` can be injected (tests); otherwise it is built from settings.
    An invalid model registry fails here, at startup, not on the first request.
    Redis / PostgreSQL connections are opened in the lifespan; if either is
    unreachable the app still starts and those features degrade (logged).
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level, secrets=settings.secret_values(), log_format=settings.log_format)
    # Production: refuse to start with missing/weak secrets or unsafe switches - BEFORE
    # any connection is opened or route is served. (No-op in development/test.)
    enforce_production_settings(settings)
    configure_client_hashing(settings.client_id_hash_key.get_secret_value() if settings.client_id_hash_key else None)
    container = build_container(settings, chat_service=chat_service)
    service = container.chat_service

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Starting %s v%s (%s)", settings.app_name, settings.app_version, settings.environment)
        logger.info(
            "LLM routing: mock_mode=%s models=%d strategy=%s",
            service.providers.mock_mode, len(service.policy.registry), service.policy.strategy,
        )
        await container.start()
        try:
            yield
        finally:
            await container.stop()
            logger.info("Shutting down %s", settings.app_name)

    docs = settings.docs_effective  # off by default in production
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    app.state.container = container
    app.state.chat_service = service
    # Starlette: the LAST added middleware is the OUTERMOST. Resulting order:
    #   RequestId -> Tracing -> SecurityHeaders -> CORS -> HTTP observability -> BodySizeLimit -> app
    # - access logs carry the request id and trace id
    # - security headers are added to every response, incl. CORS preflights and 413s
    # - metrics/access log record oversized-body rejections
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    if settings.access_log_enabled or settings.metrics_enabled:
        app.add_middleware(
            HTTPObservabilityMiddleware,
            identity=container.identity,
            metrics=container.metrics if settings.metrics_enabled else None,
            access_log=settings.access_log_enabled,
        )
    if settings.cors_allowed_origins:  # no CORS headers at all unless origins are configured
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID", "traceparent"],
            expose_headers=["X-Request-ID", "X-Trace-ID", "Retry-After", "X-RateLimit-Limit",
                            "X-RateLimit-Remaining", "X-RateLimit-Reset", "X-RateLimit-Window"],
            max_age=600,
        )
    if settings.security_headers_enabled:
        app.add_middleware(SecurityHeadersMiddleware, hsts=settings.hsts_enabled,
                           hsts_max_age=settings.hsts_max_age_seconds)
    if container.tracing.enabled:
        app.add_middleware(TracingMiddleware, tracing=container.tracing)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)

    # Root-level health (for load balancers / Docker / k8s probes)
    app.include_router(health.router)
    if settings.metrics_enabled:
        app.include_router(metrics_api.router)
    # Versioned API
    app.include_router(api_v1_router, prefix=settings.api_v1_prefix)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload=s.debug)
