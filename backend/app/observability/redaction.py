"""Centralised secret redaction for every log record (app and third-party).

Two layers, both idempotent:
  1. A LogRecord factory (installed by configure_logging) scrubs each record
     when it is created - this covers every logger and every handler, including
     handlers added later (pytest caplog, uvicorn, file handlers).
  2. `RedactionFilter` is also attached to existing handlers as defence in
     depth (e.g. records built with logging.makeLogRecord bypass the factory).

What is removed:
  - Exact values of configured secrets (API keys, passwords, admin token),
    registered at startup via `register_secrets()`.
  - Credential-shaped strings: sk-..., sk-ant-..., gsk_..., AIza..., Bearer
    tokens, Authorization / x-api-key / x-goog-api-key header values,
    api_key/password/secret/token key-value pairs, ?key=/&api_key= query
    parameters and passwords embedded in URLs (scheme://user:PASSWORD@host).

Limits (documented): pattern matching cannot recognise arbitrary free text,
so the application never logs prompts/answers in the first place; a secret
split across separate format arguments is not matched as one string.
"""

import functools
import logging
import re
from collections.abc import Iterable
from typing import Any

MASK = "***"
MIN_SECRET_LENGTH = 4  # shorter values would mangle unrelated text

def _kv_guards(*names: str) -> tuple[str, ...]:
    """'key=' / 'key:' / 'key"' / "key'" / 'key =' / 'key :' - but NOT 'input_tokens=6'."""
    return tuple(f"{n}{sep}" for n in names for sep in ("=", ":", '"', "'", " =", " :"))


# (guard substrings, pattern, replacement). A pattern runs only if one of its
# lower-cased guard substrings occurs in the lower-cased text: most log lines
# skip every regex (measured: ~240us -> a few us per typical record).
# Group 1, when present, is kept (the "key=" part).
_PATTERNS: list[tuple[tuple[str, ...], re.Pattern[str], str]] = [
    (("://",), re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]{0,20}://[^:/\s@]{0,256}:)[^@\s/]{1,512}@"),
     r"\1" + MASK + "@"),  # URL passwords
    (("bearer",), re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1" + MASK),
    (("authorization", "api-key", "x-admin-token"),
     re.compile(r"(?i)((?:authorization|proxy-authorization|x-api-key|api-key|x-goog-api-key|x-admin-token)"
                r"[\"']?\s*[:=]\s*[\"']?)(?:(?:basic|bearer|digest|token|apikey)\s+)?[^\s\"',}]+"),
     r"\1" + MASK),  # whole header value, including the auth scheme
    (("key=", "token=", "password="),
     re.compile(r"(?i)([?&](?:key|api_key|apikey|access_token|token|password)=)[^&\s\"'#]+"), r"\1" + MASK),
    (_kv_guards("key", "token") + ("password", "passwd", "secret"),
     re.compile(r"(?i)((?:api[_-]?key|password|passwd|secret|client_secret|access_token|refresh_token|"
                r"auth_token|admin_token|token)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]+"), r"\1" + MASK),
    (("sk-",), re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{8,}"), MASK),  # OpenAI / Anthropic
    (("gsk_",), re.compile(r"\bgsk_[A-Za-z0-9]{16,}"), MASK),  # Groq
    (("aiza",), re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), MASK),  # Google / Gemini
]

_secrets: list[str] = []


def register_secrets(values: Iterable[str | None]) -> None:
    """Register exact secret values to mask (longest first). Idempotent."""
    merged = set(_secrets)
    merged.update(v for v in values if v and len(v) >= MIN_SECRET_LENGTH)
    _secrets[:] = sorted(merged, key=len, reverse=True)
    redact_short_cached.cache_clear()  # cached results may predate the new secrets


def clear_registered_secrets() -> None:
    _secrets.clear()
    redact_short_cached.cache_clear()


def redact_text(text: str) -> str:
    if not text:
        return text
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, MASK)
    lowered = text.lower()
    for guards, pattern, replacement in _PATTERNS:
        if any(g in lowered for g in guards):
            text = pattern.sub(replacement, text)
            lowered = text.lower()
    return text


@functools.lru_cache(maxsize=4096)
def redact_short_cached(text: str) -> str:
    """Memoised redact_text for short, highly repetitive values (span attributes:
    model ids, tiers, routes). Pure function of (text, registered secrets); the
    cache is cleared whenever secrets change."""
    return redact_text(text)


def redact_value(value: Any) -> Any:
    """Redact strings; stringify-and-redact unknown objects (URLs, exceptions); keep numbers."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_value(v) for v in value)
    return redact_text(str(value))


_exc_formatter = logging.Formatter()


# Loggers whose formatter unpacks record.args (must keep their tuple shape).
KEEP_ARGS_LOGGERS = frozenset({"uvicorn.access"})


def _scrub_message(record: logging.LogRecord) -> None:
    """Redact the *rendered* message.

    Redacting the format string alone is unsafe: "Bearer %s" would lose its
    placeholder and break formatting. So the full message is rendered, and only
    if redaction changed it is the record rewritten:
      - normal loggers: msg = redacted text, args = None
      - KEEP_ARGS_LOGGERS: each argument redacted individually (shape kept)
    Unchanged records are left exactly as they were (zero behaviour change).
    """
    if not record.args:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        elif record.msg is not None:
            rendered = str(record.msg)
            redacted = redact_text(rendered)
            if redacted != rendered:
                record.msg = redacted
        return
    try:
        rendered = record.getMessage()
    except Exception:  # malformed call: logging will report it; still scrub what we can
        record.msg = redact_text(str(record.msg))
        record.args = tuple(redact_value(a) for a in record.args) if isinstance(record.args, tuple) else None
        return
    redacted = redact_text(rendered)
    if redacted == rendered:
        return
    if record.name in KEEP_ARGS_LOGGERS and isinstance(record.args, tuple):
        record.args = tuple(redact_value(a) for a in record.args)
    else:
        record.msg, record.args = redacted, None


def scrub_record(record: logging.LogRecord) -> logging.LogRecord:
    """Redact message, pre-rendered exception/stack text and log_event fields in place."""
    if getattr(record, "_redacted", False):
        return record
    _scrub_message(record)
    if record.exc_info and not record.exc_text:
        record.exc_text = _exc_formatter.formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = redact_text(record.exc_text)
    if record.stack_info:
        record.stack_info = redact_text(record.stack_info)
    fields = getattr(record, "fields", None)
    if isinstance(fields, dict):
        record.fields = redact_value(fields)
    record._redacted = True
    record._scrubbed_state = _state(record)
    return record


def _state(record: logging.LogRecord) -> tuple:
    return (record.msg, record.args, record.exc_info, record.exc_text, record.stack_info)


def _unchanged_since_scrub(record: logging.LogRecord) -> bool:
    """True only if every scrubbed attribute is still the very same object (identity check)."""
    saved = getattr(record, "_scrubbed_state", None)
    if not getattr(record, "_redacted", False) or saved is None:
        return False
    return all(a is b for a, b in zip(saved, _state(record)))


class RedactionFilter(logging.Filter):
    """Handler/logger filter form of the same scrubbing (never drops records)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _unchanged_since_scrub(record):
            # Never scrubbed, or modified afterwards - e.g. logging.makeLogRecord runs the
            # factory on an EMPTY record and then fills msg/args via __dict__.update().
            record._redacted = False
            scrub_record(record)
            return True
        # Same msg/args/exception/stack objects the factory scrubbed: skip re-scrubbing them.
        # `extra` attributes are attached after the factory runs; re-scrub `fields` unless
        # log_event already redacted them.
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and not getattr(record, "_fields_redacted", False):
            record.fields = redact_value(fields)
        return True
