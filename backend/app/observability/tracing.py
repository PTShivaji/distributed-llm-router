"""OpenTelemetry tracing: per-app provider, safe span helpers, W3C propagation.

Design
------
- Disabled by default (TRACING_ENABLED=false): a no-op tracer is used, spans
  are non-recording and cost ~nothing; logs keep the Step 2 format.
- Each app instance owns its OWN TracerProvider (not the process-global one),
  so tests and multiple apps in one process never share spans.
- Exporters: none (default) | memory (tests/debug) | console | otlp.
  OTLP needs the optional package in requirements-observability.txt.
- Only the W3C TraceContext propagator is used. Baggage is never read or
  propagated (it can carry arbitrary user data).
- Incoming `traceparent` is parsed by the standard propagator (strict format
  validation). It becomes the PARENT only if TRACING_TRUST_INCOMING=true;
  otherwise a new trace is started and the remote context is attached as a
  span LINK, so callers cannot force trace ids or sampling decisions.

Attribute policy
----------------
`safe_attributes()` keeps only keys in ALLOWED_ATTRIBUTES, drops None,
converts enums/objects to short strings, redacts and truncates strings.
Spans never contain prompts, responses, headers, raw paths/query strings,
client addresses, keys or passwords. Exceptions are recorded as `error.type`
(class name) only - never the exception message or stack trace.
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Link, NoOpTracer, Span, SpanKind, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.core.logging import log_event
from app.observability.redaction import redact_short_cached, redact_text

logger = logging.getLogger("app.tracing")

MAX_ATTRIBUTE_LENGTH = 128
_PROPAGATOR = TraceContextTextMapPropagator()

# Every attribute key the application may set. Anything else is dropped.
ALLOWED_ATTRIBUTES = frozenset({
    # HTTP (no url.full / url.path / url.query / client.address on purpose)
    "http.request.method", "http.route", "http.response.status_code", "url.scheme", "network.protocol.version",
    "request_id", "error.type", "error.category", "error.code", "trace.incoming_traceparent",
    # chat / classification / routing
    "chat.complexity", "chat.intent", "chat.complexity_score", "chat.confidence", "chat.is_coding",
    "chat.query_chars", "chat.override", "chat.use_cache", "chat.outcome", "chat.fallback_used",
    "chat.attempts", "chat.selected_model", "chat.model_used",
    "route.selected_model", "route.requested_tier", "route.routed_tier", "route.strategy",
    "route.override", "route.candidates", "route.fallback_chain_length",
    # provider attempts
    "llm.model", "llm.provider", "llm.adapter", "llm.attempt", "llm.retry", "llm.is_fallback",
    "llm.outcome", "llm.error_kind", "llm.retryable", "llm.timeout_s",
    "llm.usage.input_tokens", "llm.usage.output_tokens", "llm.usage.source", "llm.cost_usd",
    # cache / embeddings
    "cache.status", "cache.similarity", "cache.stored", "cache.candidates", "cache.lock_acquired",
    "cache.semantic_enabled", "cache.guard_filtered", "embedding.provider", "embedding.dimensions",
    # redis / health / rate limit
    "db.system", "db.operation", "health.backend", "health.models", "health.state", "health.model",
    "ratelimit.scope", "ratelimit.allowed", "ratelimit.window", "ratelimit.remaining", "ratelimit.degraded",
    # persistence / analytics
    "db.rows", "db.outcome", "db.queue_depth", "analytics.filtered", "analytics.total_requests",
})


def safe_attributes(attrs: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if key not in ALLOWED_ATTRIBUTES or value is None:
            continue
        if isinstance(value, Enum):
            value = value.value
        if isinstance(value, bool | int | float):
            out[key] = value
        else:
            text = str(value)[: MAX_ATTRIBUTE_LENGTH * 4]  # bound work before redaction
            # Short values repeat (model ids, tiers, routes): memoised redaction.
            redacted = redact_short_cached(text) if len(text) <= MAX_ATTRIBUTE_LENGTH else redact_text(text)
            out[key] = redacted[:MAX_ATTRIBUTE_LENGTH]
    return out


def set_attributes(span: Span, **attrs: Any) -> None:
    if span.is_recording():
        span.set_attributes(safe_attributes(attrs))


def mark_error(span: Span, error_type: str, **attrs: Any) -> None:
    """Error status with a bounded description (the error kind/type, never a message)."""
    if span.is_recording():
        span.set_attributes(safe_attributes({"error.type": error_type, **attrs}))
        span.set_status(Status(StatusCode.ERROR, description=str(error_type)[:MAX_ATTRIBUTE_LENGTH]))


class _NoopSpanContext:
    """Shared, stateless context manager used when tracing is disabled: no attribute
    sanitisation, no generator, no context attach (Phase 5 step 6B)."""

    __slots__ = ()

    def __enter__(self) -> Span:
        return trace.INVALID_SPAN  # non-recording: set_attributes/mark_error are no-ops on it

    def __exit__(self, *exc_info: object) -> bool:
        return False  # never swallow exceptions


_NOOP_SPAN_CONTEXT = _NoopSpanContext()


def start_span(
    tracer: Tracer, name: str, *, kind: SpanKind = SpanKind.INTERNAL, links: list[Link] | None = None, **attrs: Any
):
    """Current-span context manager with the safe attribute/exception policy.

    Disabled tracing (NoOpTracer) short-circuits to a shared no-op context manager;
    enabled tracing behaves exactly as before.
    """
    if isinstance(tracer, NoOpTracer):
        return _NOOP_SPAN_CONTEXT
    return _recording_span(tracer, name, kind, links, attrs)


@contextmanager
def _recording_span(
    tracer: Tracer, name: str, kind: SpanKind, links: list[Link] | None, attrs: dict[str, Any]
) -> Iterator[Span]:
    with tracer.start_as_current_span(
        name, kind=kind, links=links, attributes=safe_attributes(attrs),
        record_exception=False, set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as exc:
            mark_error(span, type(exc).__name__)
            raise


def current_span_context() -> trace.SpanContext | None:
    ctx = trace.get_current_span().get_span_context()
    return ctx if ctx.is_valid else None


def extract_remote_context(headers: Mapping[str, str]) -> trace.SpanContext | None:
    """Parse ONLY `traceparent` (+ `tracestate`) with the W3C propagator. Invalid -> None."""
    carrier = {k: v for k, v in headers.items() if k in ("traceparent", "tracestate")}
    if "traceparent" not in carrier:
        return None
    ctx = trace.get_current_span(_PROPAGATOR.extract(carrier)).get_span_context()
    return ctx if ctx.is_valid else None


class Tracing:
    """Owns this app's tracer, provider and (optional) exporter."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        exporter: str = "none",
        service_name: str = "llm-router",
        service_version: str = "unknown",
        environment: str = "development",
        sample_ratio: float = 1.0,
        trust_incoming: bool = False,
        otlp_endpoint: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.trust_incoming = trust_incoming
        self.exporter_name = exporter if enabled else "none"
        self.memory_exporter: InMemorySpanExporter | None = None
        self.provider: TracerProvider | None = None
        if not enabled:
            self.tracer: Tracer = NoOpTracer()
            return

        self.provider = TracerProvider(
            resource=Resource.create({
                "service.name": service_name, "service.version": service_version,
                "deployment.environment.name": environment,
            }),
            sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
        )
        processor = self._processor(exporter, otlp_endpoint)
        if processor is not None:
            self.provider.add_span_processor(processor)
        self.tracer = self.provider.get_tracer("app.llm_router", service_version)
        log_event(logger, "tracing.enabled", exporter=self.exporter_name, sample_ratio=sample_ratio,
                  trust_incoming=trust_incoming)

    def _processor(self, exporter: str, otlp_endpoint: str | None) -> SpanProcessor | None:
        if exporter == "memory":
            self.memory_exporter = InMemorySpanExporter()
            return SimpleSpanProcessor(self.memory_exporter)  # synchronous: deterministic for tests
        if exporter == "console":
            return BatchSpanProcessor(ConsoleSpanExporter())
        if exporter == "otlp":
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415
            except ImportError:
                log_event(logger, "tracing.exporter_unavailable", logging.WARNING, exporter="otlp",
                          hint="pip install -r requirements-observability.txt")
                self.exporter_name = "none"
                return None
            return BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint) if otlp_endpoint else OTLPSpanExporter())
        self.exporter_name = "none"
        return None  # spans are created (trace ids in logs) but not exported

    def start_request_context(self, headers: Mapping[str, str]) -> tuple[otel_context.Context | None, list[Link]]:
        """Parent context (trusted incoming traceparent) and links (untrusted one)."""
        remote = extract_remote_context(headers) if self.enabled else None
        if remote is None:
            return None, []
        if self.trust_incoming:
            return trace.set_span_in_context(trace.NonRecordingSpan(remote)), []
        return otel_context.Context(), [Link(remote, {"trace.incoming_traceparent": "untrusted"})]

    def shutdown(self) -> None:
        if self.provider is not None:
            self.provider.shutdown()


NOOP_TRACER: Tracer = NoOpTracer()
