"""HTTP hardening middleware (pure ASGI): request-size limit and security headers.

BodySizeLimitMiddleware
  - Content-Length above the limit -> 413 immediately; the body is never read.
  - No Content-Length (chunked / streamed): bytes are counted as the app reads
    them and reading stops with 413 the moment the limit is exceeded, so a
    client cannot stream an unbounded body into memory.
  - Error responses use the standard envelope (code "request_too_large").

SecurityHeadersMiddleware adds to every response:
  X-Content-Type-Options: nosniff, X-Frame-Options: DENY, Referrer-Policy: no-referrer,
  Cross-Origin-Opener-Policy / Cross-Origin-Resource-Policy: same-origin,
  Permissions-Policy (all sensitive features off), Content-Security-Policy
  (strict for the JSON API; relaxed only for the interactive docs pages),
  Cache-Control: no-store on /api/* (answers may be sensitive),
  Strict-Transport-Security only when HSTS_ENABLED (HTTPS deployments).
"""

from fastapi import HTTPException, status
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# FastAPI's Swagger UI / ReDoc load assets from a CDN; only these pages get this policy.
DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
            "font-src 'self' https://fonts.gstatic.com; worker-src 'self' blob:; frame-ancestors 'none'")
DOCS_PATHS = ("/docs", "/redoc")

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
}


class RequestTooLargeError(HTTPException):
    def __init__(self, limit: int) -> None:
        super().__init__(status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                         detail=f"Request body exceeds the {limit}-byte limit.")


def security_headers(path: str, *, hsts: bool, hsts_max_age: int) -> dict[str, str]:
    headers = dict(BASE_HEADERS)
    headers["Content-Security-Policy"] = DOCS_CSP if path.startswith(DOCS_PATHS) else API_CSP
    if path.startswith("/api/"):
        headers["Cache-Control"] = "no-store"
    if hsts:
        headers["Strict-Transport-Security"] = f"max-age={hsts_max_age}; includeSubDomains"
    return headers


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False, hsts_max_age: int = 31_536_000) -> None:
        self.app = app
        self.hsts = hsts
        self.hsts_max_age = hsts_max_age

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = security_headers(scope.get("path", ""), hsts=self.hsts, hsts_max_age=self.hsts_max_age)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in extra.items():
                    if name not in headers:  # never override a header an endpoint set deliberately
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = False  # malformed header: the server/parser rejects it
            if too_large:
                # The first attempt to read the body raises 413 inside the app, so the normal
                # error handler builds the envelope and metrics/access log record it. Not one
                # byte of the oversized body is read.
                await self._run_rejecting(scope, send)
                return

        received = 0
        max_bytes = self.max_bytes

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise RequestTooLargeError(max_bytes)
            return message

        await self.app(scope, limited_receive, send)

    async def _run_rejecting(self, scope: Scope, send: Send) -> None:
        """Let the app's routing/exception stack produce the 413 without reading the body."""
        max_bytes = self.max_bytes

        async def refuse_body() -> Message:
            raise RequestTooLargeError(max_bytes)

        await self.app(scope, refuse_body, send)
