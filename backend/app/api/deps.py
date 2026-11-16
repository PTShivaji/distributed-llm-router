"""Shared API dependencies: container access, chat auth, rate limiting, admin auth.

Three SEPARATE credentials (Settings rejects any reuse between them):
  CHAT_API_KEYS            -> POST /api/v1/chat           (authenticate_chat)
  OBSERVABILITY_API_TOKEN  -> metrics/analytics/stats      (require_observability_token)
  ADMIN_API_TOKEN          -> DELETE /api/v1/cache         (require_admin)
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass

from fastapi import Request, Response, status

from app.core.errors import AppError
from app.core.logging import log_event
from app.infra.container import Container
from app.ratelimit.limiter import hash_subject
from app.observability.tracing import set_attributes, start_span

logger = logging.getLogger("app.api")


class RateLimitExceededError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limit_exceeded"


class RateLimiterUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "rate_limiter_unavailable"


class AdminDisabledError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "admin_disabled"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


def get_container(request: Request) -> Container:
    return request.app.state.container


async def enforce_client_rate_limit(request: Request, response: Response) -> str:
    """Per-client limits shared by all instances. Returns the resolved client id."""
    client_id = get_container(request).identity.resolve(request)
    return await _apply_client_rate_limit(request, response, client_id)


async def _apply_client_rate_limit(request: Request, response: Response, client_id: str) -> str:
    container = get_container(request)
    request.state.client_id = client_id
    limiter = container.rate_limiter
    if limiter is None:
        return client_id

    with start_span(container.tracing.tracer, "ratelimit.check", **{"ratelimit.scope": "client",
                                                                   "db.system": "redis"}) as span:
        result = await limiter.check_client(client_id)
        set_attributes(span, **{"ratelimit.allowed": result.allowed, "ratelimit.window": result.window,
                                "ratelimit.remaining": result.remaining, "ratelimit.degraded": result.degraded})
    if result.busy:
        raise RateLimiterUnavailableError("Rate limiter busy; retry shortly.", headers={"Retry-After": "1"})
    if result.degraded and not result.allowed:
        raise RateLimiterUnavailableError("Rate limiter unavailable; request rejected (RATE_LIMIT_FAIL_OPEN=false).")
    headers = result.headers()
    if not result.allowed:
        container.metrics.rate_limit_rejected("client", result.window)
        log_event(logger, "ratelimit.rejected", logging.WARNING, client=hash_subject(client_id)[:12],
                  window=result.window, limit=result.limit, retry_after=result.reset_seconds)
        raise RateLimitExceededError(
            f"Rate limit exceeded: {result.limit} requests per {result.window}.",
            details={"limit": result.limit, "window": result.window, "retry_after_seconds": result.reset_seconds},
            headers=headers,
        )
    response.headers.update(headers)
    return client_id


def _bearer_matches(request: Request, expected: str) -> bool:
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(supplied.encode(), expected.encode())


async def require_observability_token(request: Request) -> None:
    """Optional bearer token for observability endpoints:
    GET /metrics, /api/v1/analytics/summary, /api/v1/cache/stats, /health/dependencies.

    OBSERVABILITY_API_TOKEN unset -> open (development default);
    set -> "Authorization: Bearer <token>" required (constant-time comparison).
    Production refuses to start without it (app/core/production.py).
    """
    token = get_container(request).settings.observability_api_token
    if token is None:
        return
    if not _bearer_matches(request, token.get_secret_value()):
        log_event(logger, "observability.unauthorized", logging.WARNING, path=request.url.path)
        raise UnauthorizedError("Missing or invalid observability token.", headers={"WWW-Authenticate": "Bearer"})


async def require_admin(request: Request) -> None:
    """Static bearer-token check for admin endpoints.

    This is a single shared secret (ADMIN_API_TOKEN), not user authentication.
    If no token is configured, admin endpoints are disabled entirely.
    """
    token = get_container(request).settings.admin_api_token
    if token is None:
        raise AdminDisabledError("Admin endpoints are disabled. Set ADMIN_API_TOKEN to enable them.")
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.encode(), token.get_secret_value().encode()):
        log_event(logger, "admin.unauthorized", logging.WARNING, path=request.url.path)
        raise UnauthorizedError("Missing or invalid admin token.", headers={"WWW-Authenticate": "Bearer"})


# --------------------------------------------------------------------------- #
# Chat authentication (Phase 5 step 8)
@dataclass(frozen=True, slots=True)
class ChatPrincipal:
    """Who is calling /chat. `name` is the CHAT_API_KEYS client name (never the key)."""

    name: str | None
    authenticated: bool

    @property
    def client_id(self) -> str | None:
        return f"chat-key:{self.name}" if self.name else None


ANONYMOUS = ChatPrincipal(None, False)
_CHAT_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="chat"'}


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


async def authenticate_chat(request: Request) -> ChatPrincipal:
    """CHAT_AUTH_MODE=none -> anonymous; api_key -> a valid "Authorization: Bearer <key>".

    Every configured key is compared (constant time, fixed-length digests, no early
    exit), so response timing does not reveal which keys exist or how much matched.
    """
    settings = get_container(request).settings
    if settings.chat_auth_mode == "none":
        return ANONYMOUS
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    supplied = supplied.strip()
    if scheme.lower() != "bearer" or not supplied:
        log_event(logger, "chat.unauthorized", logging.WARNING, reason="missing_credentials", path=request.url.path)
        raise UnauthorizedError("Missing chat API key: send it as a Bearer token in the Authorization header.", headers=_CHAT_CHALLENGE)
    supplied_digest = _digest(supplied)
    matched: str | None = None
    for name, key in settings.chat_api_keys.items():
        if hmac.compare_digest(supplied_digest, _digest(key.get_secret_value())) and matched is None:
            matched = name
    if matched is None:
        log_event(logger, "chat.unauthorized", logging.WARNING, reason="invalid_credentials", path=request.url.path)
        raise UnauthorizedError("Invalid chat API key.", headers=_CHAT_CHALLENGE)
    request.state.chat_client = matched
    return ChatPrincipal(matched, True)


async def enforce_chat_access(request: Request, response: Response) -> str:
    """Authenticate first (401 costs no quota), then apply per-client rate limits.

    Authenticated callers are limited per API key (all their IPs share one quota);
    anonymous callers (CHAT_AUTH_MODE=none) per client address, as before.
    """
    principal = await authenticate_chat(request)
    client_id = principal.client_id or get_container(request).identity.resolve(request)
    return await _apply_client_rate_limit(request, response, client_id)
