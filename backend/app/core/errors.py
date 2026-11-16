"""Centralised exception handling and error categorisation.

Every error response uses the same JSON envelope:

    {"error": {"code": "<machine_code>", "message": "<human text>", "details": ...}}

Each handled error is also assigned a bounded *category* (see ERROR_CATEGORIES)
which is stored on the request scope so the access log (and later metrics)
can report it without parsing response bodies.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.request_context import REQUEST_ID_HEADER, bind_request_id

logger = logging.getLogger(__name__)

# code -> category. Categories are a small fixed set (safe as metric labels).
ERROR_CATEGORIES: dict[str, str] = {
    "validation_error": "validation",
    "request_too_large": "validation",
    "rate_limit_exceeded": "rate_limited",
    "unauthorized": "auth",
    "forbidden": "auth",
    "admin_disabled": "auth",
    "not_found": "not_found",
    "method_not_allowed": "client",
    "unknown_model": "client",
    "model_disabled": "client",
    "invalid_time_range": "client",
    "all_models_failed": "provider",
    "request_rejected_by_provider": "provider",
    "no_model_available": "routing",
    "cache_unavailable": "dependency",
    "database_unavailable": "dependency",
    "rate_limiter_unavailable": "dependency",
    "budget_unavailable": "dependency",
    "budget_exceeded": "rate_limited",
    "internal_error": "internal",
}
CATEGORY_VALUES = frozenset(ERROR_CATEGORIES.values()) | {"client", "server"}


def categorize(status_code: int, code: str) -> str:
    if code in ERROR_CATEGORIES:
        return ERROR_CATEGORIES[code]
    return "server" if status_code >= 500 else "client"


class AppError(Exception):
    """Base class for expected, domain-level errors raised by the application."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "app_error"

    def __init__(self, message: str, *, details: Any = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.headers = headers or {}


MAX_ERROR_MSG_LENGTH = 300
_PRIMITIVES = (str, int, float, bool)


def sanitize_validation_errors(errors: Any) -> list[dict[str, Any]]:
    """Validation details WITHOUT the submitted values (Phase 5 step 7).

    Pydantic's raw errors echo the rejected `input` (which can be a secret, a
    prompt or a multi-megabyte body) and a docs `url`. Only `type`, `loc`, `msg`
    and primitive `ctx` values (e.g. max_length) are returned.
    """
    clean: list[dict[str, Any]] = []
    for err in errors or []:
        item: dict[str, Any] = {
            "type": str(err.get("type", "value_error")),
            "loc": [p if isinstance(p, int) else str(p)[:100] for p in err.get("loc", ())][:10],
            "msg": str(err.get("msg", ""))[:MAX_ERROR_MSG_LENGTH],
        }
        ctx = {k: v for k, v in (err.get("ctx") or {}).items()
               if isinstance(v, _PRIMITIVES) and not (isinstance(v, str) and len(v) > 100)}
        if ctx:
            item["ctx"] = ctx
        clean.append(item)
    return clean[:50]


def _error_response(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body))


def _mark(request: Request, status_code: int, code: str) -> str:
    category = categorize(status_code, code)
    request.state.error_code = code
    request.state.error_category = category
    return category


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    category = _mark(request, exc.status_code, exc.code)
    logger.warning(
        "AppError on %s %s: %s (category=%s code=%s status=%d)",
        request.method, request.url.path, exc.message, category, exc.code, exc.status_code,
    )
    response = _error_response(exc.status_code, exc.code, exc.message, exc.details)
    response.headers.update(getattr(exc, "headers", None) or {})
    return response


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    codes = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 403: "forbidden",
             413: "request_too_large"}
    code = codes.get(exc.status_code, "http_error")
    _mark(request, exc.status_code, code)
    response = _error_response(exc.status_code, code, str(exc.detail))
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    _mark(request, status.HTTP_422_UNPROCESSABLE_CONTENT, "validation_error")
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_error",
        "Request validation failed",
        sanitize_validation_errors(exc.errors()),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Runs in Starlette's outermost ServerErrorMiddleware, i.e. AFTER our
    middlewares have unwound - so the request id is re-bound from the scope."""
    request_id = getattr(request.state, "request_id", None)
    category = _mark(request, 500, "internal_error")
    with bind_request_id(request_id):
        # Full traceback server-side (redacted by the logging layer); never sent to the client.
        logger.exception(
            "Unhandled error on %s %s (category=%s error_type=%s)",
            request.method, request.url.path, category, type(exc).__name__,
        )
    response = _error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", "An unexpected error occurred")
    if request_id:
        response.headers[REQUEST_ID_HEADER] = request_id
    # This response is produced by Starlette's outermost ServerErrorMiddleware, i.e.
    # outside our middleware chain, so security headers are applied here explicitly.
    from app.core.security import security_headers  # local import: avoids a cycle

    for name, value in security_headers(request.url.path, hsts=False, hsts_max_age=0).items():
        response.headers.setdefault(name, value)
    return response


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
