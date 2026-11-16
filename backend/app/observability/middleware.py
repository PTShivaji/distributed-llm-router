"""HTTP observability middleware (pure ASGI): structured access log + HTTP metrics.

Per request it records:
  - one `http.request` log event: method, route (template, e.g. /api/v1/chat;
    "unmatched" for 404s), status, duration_ms, client (keyed/unkeyed SHA-256
    prefix - never the raw IP), error_category / error_code when it failed
  - Prometheus: http requests, latency histogram, in-flight gauge, error categories

Never recorded: query strings, headers (incl. Authorization), bodies, raw IPs.
Must be installed INSIDE RequestIdMiddleware so the request id is bound.
"""

import logging
import time
from typing import TYPE_CHECKING

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import log_event
from app.core.request_context import scope_request_id
from app.observability.metrics import Metrics
from app.observability.tracing import mark_error, safe_attributes, set_attributes
from app.ratelimit.limiter import ClientIdentityResolver, hash_subject

if TYPE_CHECKING:
    from app.observability.tracing import Tracing

TRACE_ID_HEADER = "X-Trace-ID"

logger = logging.getLogger("app.access")


def route_template(scope: Scope) -> str:
    """Full route template, e.g. "/api/v1/models/{mid}"; "unmatched" if no route matched.

    FastAPI 0.16x nests included routers, so scope["route"].path is relative to
    the router prefix. The literal prefix is recovered from the actual path by
    rendering the route's own template with the matched path params. Prefixes
    come from code, so the label set stays bounded.
    """
    route = scope.get("route")
    relative = getattr(route, "path", None)
    if not relative:
        return "unmatched"
    path: str = scope.get("path", "")
    fmt = getattr(route, "path_format", relative)
    try:
        rendered = fmt.format(**{k: str(v) for k, v in (scope.get("path_params") or {}).items()})
    except (KeyError, IndexError, ValueError):
        return relative
    if rendered and path.endswith(rendered):
        return path[: len(path) - len(rendered)] + relative
    return relative


class HTTPObservabilityMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        identity: ClientIdentityResolver | None = None,
        metrics: Metrics | None = None,
        access_log: bool = True,
    ) -> None:
        self.app = app
        self.identity = identity or ClientIdentityResolver()
        self.metrics = metrics
        self.access_log = access_log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        if self.metrics is not None and self.metrics.enabled:
            self.metrics.http_in_progress.inc()
        failed = False
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            failed = True  # unhandled: Starlette's ServerErrorMiddleware will answer 500
            raise
        finally:
            if self.metrics is not None and self.metrics.enabled:
                self.metrics.http_in_progress.dec()
            self._finish(scope, 500 if failed else status_holder["status"], time.perf_counter() - started)

    def _finish(self, scope: Scope, status: int, seconds: float) -> None:
        state = scope.get("state") or {}
        route = route_template(scope)
        category = state.get("error_category") or ("internal" if status >= 500 else None)
        code = state.get("error_code")
        if self.metrics is not None:
            self.metrics.observe_http(scope.get("method", ""), route, status, seconds, category, code)
        if not self.access_log:
            return
        level = logging.ERROR if status >= 500 else (logging.WARNING if status in (401, 403, 429) else logging.INFO)
        if not logger.isEnabledFor(level):
            return  # skip client resolution/hashing for a record that would not be emitted
        client = self.identity.resolve(Request(scope))
        log_event(
            logger, "http.request", level,
            method=scope.get("method"),
            route=route,
            status=status,
            duration_ms=round(seconds * 1000, 2),
            client=hash_subject(client)[:12],
            error_category=category,
            error_code=code,
        )


# Backwards-compatible name from Phase 5 step 2.
AccessLogMiddleware = HTTPObservabilityMiddleware


class TracingMiddleware:
    """Root SERVER span per HTTP request (pure ASGI, only installed when tracing is enabled).

    Order: RequestIdMiddleware -> TracingMiddleware -> HTTPObservabilityMiddleware,
    so the access log / metrics run inside the span (their logs carry trace_id).
    Span name is "<METHOD> <route template>"; attributes follow tracing.ALLOWED_ATTRIBUTES
    (no raw path, query string, headers or client address). When a span is recorded
    the response carries `X-Trace-ID` (additive header).
    """

    def __init__(self, app: ASGIApp, tracing: "Tracing") -> None:
        self.app = app
        self.tracing = tracing

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.tracing.enabled:
            await self.app(scope, receive, send)
            return

        headers = {}
        for raw_key, raw_value in scope.get("headers") or []:
            key = raw_key.decode("latin-1").lower()
            if key in ("traceparent", "tracestate"):
                headers[key] = raw_value.decode("latin-1")
        parent, links = self.tracing.start_request_context(headers)
        method = scope.get("method", "")
        span = self.tracing.tracer.start_span(
            f"HTTP {method}", context=parent, kind=SpanKind.SERVER, links=links,
            attributes=safe_attributes({
                "http.request.method": method, "url.scheme": scope.get("scheme"),
                "network.protocol.version": scope.get("http_version"),
                "request_id": scope_request_id(scope),
            }),
        )
        token = otel_context.attach(trace.set_span_in_context(span))
        trace_id = format(span.get_span_context().trace_id, "032x") if span.get_span_context().is_valid else None
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                if trace_id:
                    MutableHeaders(scope=message)[TRACE_ID_HEADER] = trace_id
            await send(message)

        failed = False
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException as exc:
            failed = True
            mark_error(span, type(exc).__name__)
            raise
        finally:
            status = 500 if failed else status_holder["status"]
            route = route_template(scope)
            state = scope.get("state") or {}
            span.update_name(f"{method} {route}")
            set_attributes(span, **{"http.route": route, "http.response.status_code": status,
                                    "error.category": state.get("error_category"), "error.code": state.get("error_code")})
            if status >= 500 and not failed:
                mark_error(span, str(status), **{"error.category": state.get("error_category") or "internal"})
            span.end()
            otel_context.detach(token)
