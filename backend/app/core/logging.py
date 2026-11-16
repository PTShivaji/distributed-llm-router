"""Logging setup, structured event helper and secret redaction.

Every log record (from any logger, app or third-party) gets:
  - request_id   current X-Request-ID ("-" outside a request)
  - trace_id     current OpenTelemetry trace id ("-" when no active trace)
  - secrets redacted (see app.observability.redaction)

Text format is unchanged from earlier phases; " trace=<id>" is appended to
the rid field only when a trace is active. LOG_FORMAT=json emits one JSON
object per line instead.
"""

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from app.core.request_context import get_request_id
from app.observability.redaction import (
    RedactionFilter,
    redact_text,
    redact_value,
    register_secrets,
    scrub_record,
)

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | rid=%(request_id)s%(trace_suffix)s | %(message)s"
_factory_installed = False
MAX_LOGGED_MESSAGE = 300

# Third-party loggers whose existing handlers also get the redaction filter.
THIRD_PARTY_LOGGERS = (
    "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi",
    "LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "litellm",
    "httpx", "httpx2", "httpcore", "openai", "anthropic",
    "sqlalchemy", "sqlalchemy.engine", "redis",
)

try:  # opentelemetry-api is installed (via LiteLLM); tolerate its absence anyway
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover
    _otel_trace = None


def current_trace_id() -> str | None:
    if _otel_trace is None:
        return None
    ctx = _otel_trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def _install_record_factory() -> None:
    """Attach correlation ids and redact *every* log record, whichever handler formats it."""
    global _factory_installed
    if _factory_installed:
        return
    base_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base_factory(*args, **kwargs)
        record.request_id = get_request_id()
        trace_id = current_trace_id()
        record.trace_id = trace_id or "-"
        record.trace_suffix = f" trace={trace_id}" if trace_id else ""
        return scrub_record(record)

    logging.setLogRecordFactory(factory)
    _factory_installed = True


class ClientAddressFilter(logging.Filter):
    """uvicorn's access log passes the raw client "ip:port" as args[0]; replace it with a hash."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args and isinstance(record.args[0], str):
            from app.ratelimit.limiter import hash_subject  # local: avoid import cycle

            host = record.args[0].rsplit(":", 1)[0]
            record.args = (f"client-{hash_subject(host)[:12]}", *record.args[1:])
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Values were already redacted by the record factory."""

    def format(self, record: logging.LogRecord) -> str:
        body: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "trace_id": getattr(record, "trace_id", "-"),
            "message": record.getMessage(),
        }
        if event := getattr(record, "event", None):
            body["event"] = event
            body["fields"] = getattr(record, "fields", {})
        if record.exc_info or record.exc_text:
            body["exception"] = record.exc_text or self.formatException(record.exc_info)
        return json.dumps(body, default=str)


def _attach_filters() -> None:
    redaction = RedactionFilter()
    loggers = [logging.getLogger(), *(logging.getLogger(n) for n in THIRD_PARTY_LOGGERS)]
    for lg in loggers:
        for handler in lg.handlers:
            if not any(isinstance(f, RedactionFilter) for f in handler.filters):
                handler.addFilter(redaction)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, ClientAddressFilter) for f in access.filters):
        access.addFilter(ClientAddressFilter())


def configure_logging(level: str = "INFO", *, secrets: Iterable[str | None] = (), log_format: str = "text") -> None:
    _install_record_factory()
    register_secrets(secrets)
    root = logging.getLogger()
    had_handlers = bool(root.handlers)
    logging.basicConfig(level=level, format=_FORMAT)
    if log_format == "json" and not had_handlers:
        for handler in root.handlers:
            handler.setFormatter(JsonFormatter())
    _attach_filters()


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact known secret values and credential-shaped strings, then truncate.

    Used for provider error messages that are stored/returned in diagnostics.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    text = redact_text(text)
    return text if len(text) <= MAX_LOGGED_MESSAGE else text[:MAX_LOGGED_MESSAGE] + "...[truncated]"


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    text = str(value)
    return f'"{text}"' if (" " in text or not text) else text


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit `event key=value ...` and attach (redacted) fields to the record for JSON handlers.

    Callers must never pass raw user text or secrets as fields; redaction is a
    safety net, not a licence.

    Disabled levels return before any redaction/formatting work (measured cost
    ~0.5 ms/request at WARNING before Phase 5 step 6B). Emitted records are
    redacted exactly as before.
    """
    if not logger.isEnabledFor(level):
        return
    fields = redact_value(fields)
    message = " ".join([event, *(f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None)])
    logger.log(level, message, extra={"event": event, "fields": fields, "_fields_redacted": True})
