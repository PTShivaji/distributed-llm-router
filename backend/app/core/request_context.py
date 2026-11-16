"""Per-request correlation id (X-Request-ID) shared by logs and responses."""

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    return _request_id.get()


@contextmanager
def bind_request_id(request_id: str | None) -> Iterator[None]:
    """Temporarily set the request id for code running outside the request's
    own context (exception handlers after middleware unwound, background workers)."""
    if not request_id:
        yield
        return
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


def scope_request_id(scope: Scope) -> str | None:
    state = scope.get("state")
    return state.get("request_id") if isinstance(state, dict) else None


class RequestIdMiddleware:
    """Pure ASGI middleware: accepts a safe incoming X-Request-ID or generates one."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id.set(request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _request_id.reset(token)
