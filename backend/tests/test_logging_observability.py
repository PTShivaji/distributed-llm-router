"""Phase 5 step 2: log redaction, structured access log, error categories, correlation."""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from app.core.config import Settings
from app.core.errors import CATEGORY_VALUES, ERROR_CATEGORIES, categorize
from app.core.logging import _FORMAT, ClientAddressFilter, JsonFormatter, configure_logging, log_event
from app.main import create_app
from app.observability.redaction import (
    MASK,
    RedactionFilter,
    clear_registered_secrets,
    redact_text,
    register_secrets,
    scrub_record,
)
from app.ratelimit.limiter import configure_client_hashing, hash_subject

CHAT = "/api/v1/chat"


@pytest.fixture(autouse=True)
def _logging_installed():
    configure_logging("INFO")  # installs the record factory (idempotent)
    yield


def all_text(records: list[logging.LogRecord]) -> str:
    """Everything a handler could emit: messages, rendered exceptions, stack info, fields."""
    parts = []
    for r in records:
        parts.append(r.getMessage())
        parts.append(r.exc_text or "")
        parts.append(r.stack_info or "")
        parts.append(json.dumps(getattr(r, "fields", {}), default=str))
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Redaction: patterns and registered values
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw, leaked",
    [
        ("Incorrect API key provided: sk-proj-abcdefghijklmnop", "sk-proj-abcdefghijklmnop"),
        ("anthropic key sk-ant-api03-ABCDEFGHIJKLMNOP", "sk-ant-api03-ABCDEFGHIJKLMNOP"),
        ("groq gsk_ABCDEFGHIJKLMNOPQRSTUV", "gsk_ABCDEFGHIJKLMNOPQRSTUV"),
        ("gemini AIzaSyA1234567890abcdefghijk", "AIzaSyA1234567890abcdefghijk"),
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi.payload.sig"),
        ("headers={'authorization': 'Basic dXNlcjpwYXNz'}", "dXNlcjpwYXNz"),
        ("x-api-key: anthropic-raw-key-123", "anthropic-raw-key-123"),
        ("x-goog-api-key=goog-raw-key-456", "goog-raw-key-456"),
        ('{"api_key": "json-key-789"}', "json-key-789"),
        ("password=hunter2hunter2", "hunter2hunter2"),
        ("client_secret: cs-value-000", "cs-value-000"),
        ("POST https://generativelanguage.googleapis.com/v1/x?alt=sse&key=QUERYKEY12345", "QUERYKEY12345"),
        ("connect postgresql+psycopg://router:pg-secret-pw@db:5432/x", "pg-secret-pw"),
        ("redis://:redis-secret-pw@localhost:6379/0", "redis-secret-pw"),
    ],
)
def test_credential_patterns_are_masked(raw, leaked):
    out = redact_text(raw)
    assert leaked not in out and MASK in out


@pytest.mark.parametrize(
    "safe",
    [
        "chat.completed model_used=mock-fast input_tokens=6 output_tokens=50 total_tokens=56 latency_ms=1.5",
        "provider.invoke model=mock-fast provider=mock adapter=mock attempt=1 timeout_s=10",
        'HTTP Request: POST http://testserver/api/v1/chat "HTTP/1.1 200 OK"',
        "max_output_tokens=1024 usage_source=estimated cache_status=hit_exact",
        "redis://localhost:6379/15",
    ],
)
def test_ordinary_log_lines_are_untouched(safe):
    assert redact_text(safe) == safe


def test_registered_secret_values_are_masked_even_without_a_pattern():
    register_secrets(["mistral-style-key-without-prefix-0001"])
    try:
        assert redact_text("key mistral-style-key-without-prefix-0001 used") == "key *** used"
    finally:
        clear_registered_secrets()


def test_very_short_values_are_not_registered():
    register_secrets(["pw", "abc"])
    try:
        assert redact_text("pw abc") == "pw abc"
    finally:
        clear_registered_secrets()


def test_scrub_redacts_rendered_message_with_object_args():
    class URL:
        def __str__(self):
            return "https://api.example.com/v1?key=OBJECTKEY999"

    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: %s %s %d", ("POST", URL(), 200), None)
    scrub_record(record)
    assert record.getMessage() == "HTTP Request: POST https://api.example.com/v1?key=*** 200"


def test_placeholder_inside_a_secret_pattern_does_not_break_formatting():
    record = logging.LogRecord("LiteLLM", logging.DEBUG, __file__, 1, "headers: Authorization: Bearer %s", ("tok-123456789",), None)
    scrub_record(record)
    assert record.getMessage() == "headers: Authorization: ***"


def test_unchanged_records_keep_their_args():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "count=%d model=%s", (3, "m"), None)
    scrub_record(record)
    assert record.args == (3, "m") and record.getMessage() == "count=3 model=m"


def test_uvicorn_access_args_keep_shape_but_are_redacted():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("client-abc", "GET", "/x?api_key=ACCESSKEY77", "1.1", 200), None)
    scrub_record(record)
    assert isinstance(record.args, tuple) and len(record.args) == 5 and record.args[4] == 200
    assert "ACCESSKEY77" not in record.getMessage()


def test_exception_tracebacks_are_redacted():
    try:
        raise RuntimeError("upstream said: invalid key sk-proj-TRACEBACKSECRET1")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    scrub_record(record)
    assert "TRACEBACKSECRET1" not in record.exc_text and "RuntimeError" in record.exc_text


def test_filter_covers_records_that_bypass_the_factory():
    record = logging.makeLogRecord({"msg": "token: bypass-secret-token", "levelno": logging.INFO})
    RedactionFilter().filter(record)
    assert "bypass-secret-token" not in record.getMessage()


@pytest.mark.parametrize("attr, value", [
    ("msg", "password=mutated-secret-1"),
    ("args", ("sk-proj-MUTATEDARGSECRET2",)),
    ("exc_text", "Traceback ... sk-proj-MUTATEDEXCSECRET3"),
    ("stack_info", "Stack ... sk-proj-MUTATEDSTACKSECRET4"),
])
def test_filter_rescrubs_records_mutated_after_creation(caplog, attr, value):
    """Step 6B regression guard: the filter's fast path must not trust a record whose
    scrubbed attributes were replaced after the factory ran."""
    record = logging.getLogger("app.test").makeRecord("app.test", logging.INFO, __file__, 1, "%s" if attr == "args" else "clean", (), None)
    setattr(record, attr, value)
    RedactionFilter().filter(record)
    rendered = " ".join(str(x) for x in (record.getMessage(), record.exc_text, record.stack_info))
    assert "MUTATED" not in rendered and "mutated-secret-1" not in rendered


def test_filter_fast_path_still_scrubs_extra_fields():
    record = logging.getLogger("app.test").makeRecord(
        "app.test", logging.INFO, __file__, 1, "clean", (), None, extra={"fields": {"detail": "token: extra-secret-5"}})
    RedactionFilter().filter(record)
    assert "extra-secret-5" not in json.dumps(record.fields)


def test_log_event_skips_all_work_when_level_disabled(monkeypatch):
    import app.core.logging as L

    calls = []
    monkeypatch.setattr(L, "redact_value", lambda v: calls.append(v) or v)
    quiet = logging.getLogger("app.test.quiet")
    quiet.setLevel(logging.WARNING)
    try:
        L.log_event(quiet, "x.disabled", detail="anything")
        assert calls == []                      # no redaction work for a disabled level
        L.log_event(quiet, "x.enabled", logging.ERROR, detail="token: shown-secret-6")
        assert len(calls) == 1                  # enabled level: redacted as before
    finally:
        quiet.setLevel(logging.NOTSET)


@pytest.mark.parametrize("name", ["LiteLLM", "litellm", "httpx", "httpx2", "httpcore", "openai", "anthropic", "uvicorn.error"])
def test_third_party_debug_logs_are_redacted(caplog, name):
    secret = "sk-proj-THIRDPARTYDEBUG99"
    with caplog.at_level(logging.DEBUG, logger=name):
        logging.getLogger(name).debug("request headers {'Authorization': 'Bearer %s'} body=%s", secret, {"api_key": secret})
    assert caplog.records and secret not in all_text(caplog.records)


def test_log_event_fields_are_redacted(caplog):
    with caplog.at_level(logging.INFO):
        log_event(logging.getLogger("app.test"), "x.event", detail="AuthenticationError: sk-proj-FIELDSECRET0001")
    rec = caplog.records[-1]
    assert "FIELDSECRET0001" not in rec.getMessage()
    assert "FIELDSECRET0001" not in json.dumps(rec.fields)


# --------------------------------------------------------------------------- #
# Format / trace id / JSON
# --------------------------------------------------------------------------- #
def _format(record: logging.LogRecord) -> str:
    return logging.Formatter(_FORMAT).format(record)


def test_text_format_unchanged_without_trace(caplog):
    with caplog.at_level(logging.INFO):
        logging.getLogger("app.test").info("hello")
    line = _format(caplog.records[-1])
    assert "| rid=- | hello" in line and "trace=" not in line


def test_trace_id_added_when_a_span_is_active(caplog):
    ctx = SpanContext(trace_id=0x1234ABCD, span_id=0x42, is_remote=False, trace_flags=TraceFlags(1))
    with caplog.at_level(logging.INFO), trace.use_span(NonRecordingSpan(ctx)):
        logging.getLogger("app.test").info("inside span")
    rec = caplog.records[-1]
    assert rec.trace_id == format(0x1234ABCD, "032x")
    assert f"trace={rec.trace_id} | inside span" in _format(rec)


def test_json_formatter(caplog):
    with caplog.at_level(logging.INFO):
        log_event(logging.getLogger("app.test"), "x.json", model="m", detail="password=jsonsecret1")
    body = json.loads(JsonFormatter().format(caplog.records[-1]))
    assert body["event"] == "x.json" and body["fields"]["model"] == "m"
    assert body["request_id"] == "-" and body["trace_id"] == "-"
    assert "jsonsecret1" not in json.dumps(body)


def test_uvicorn_access_log_client_address_is_hashed():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("203.0.113.9:51515", "GET", "/health", "1.1", 200), None)
    ClientAddressFilter().filter(record)
    message = record.getMessage()
    assert "203.0.113.9" not in message and message.startswith("client-")
    assert message.startswith(f"client-{hash_subject('203.0.113.9')[:12]}")


def test_keyed_client_hashing():
    plain = hash_subject("203.0.113.9")
    configure_client_hashing("deployment-secret-key")
    try:
        keyed = hash_subject("203.0.113.9")
        assert keyed != plain and keyed == hash_subject("203.0.113.9")
    finally:
        configure_client_hashing(None)
    assert hash_subject("203.0.113.9") == plain


# --------------------------------------------------------------------------- #
# Error categories
# --------------------------------------------------------------------------- #
def test_every_known_code_has_a_bounded_category():
    assert set(ERROR_CATEGORIES.values()) <= CATEGORY_VALUES
    assert categorize(418, "teapot") == "client" and categorize(502, "weird") == "server"
    assert categorize(429, "rate_limit_exceeded") == "rate_limited"


# --------------------------------------------------------------------------- #
# Access log + correlation over HTTP (hermetic)
# --------------------------------------------------------------------------- #
def access_events(records):
    return [r for r in records if getattr(r, "event", None) == "http.request"]


def test_access_log_event_fields_and_correlation(client, caplog):
    with caplog.at_level(logging.INFO):
        r = client.get("/api/v1/models?filter=supersecretquery", headers={"Authorization": "Bearer hdr-secret-123456"})
    (event,) = access_events(caplog.records)
    f = event.fields
    assert (f["method"], f["route"], f["status"]) == ("GET", "/api/v1/models", 200)
    assert f["duration_ms"] >= 0 and len(f["client"]) == 12
    assert event.request_id == r.headers["X-Request-ID"]
    # Server-side loggers only for the query string: the *test client's* own httpx
    # log naturally contains the URL it requested.
    server_text = all_text([rec for rec in caplog.records if not rec.name.startswith("httpx")])
    assert "supersecretquery" not in server_text
    text = all_text(caplog.records)
    for leaked in ("hdr-secret-123456", "testclient"):
        assert leaked not in text


def test_access_log_unmatched_route_and_error_category(client, caplog):
    with caplog.at_level(logging.INFO):
        client.get("/definitely/not/here/12345")
        client.post(CHAT, json={})
        client.post(CHAT, json={"query": "hi", "model": "no-such-model"})
    events = access_events(caplog.records)
    assert [(e.fields["route"], e.fields["status"], e.fields["error_category"]) for e in events] == [
        ("unmatched", 404, "not_found"),
        ("/api/v1/chat", 422, "validation"),
        ("/api/v1/chat", 400, "client"),
    ]
    assert events[2].fields["error_code"] == "unknown_model"


def test_unhandled_500_log_keeps_request_id_and_redacts(test_settings, caplog):
    app = create_app(test_settings)

    @app.get("/_explode")
    async def explode():
        raise RuntimeError("provider rejected key sk-proj-EXPLODESECRET42")

    with caplog.at_level(logging.INFO), TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/_explode", headers={"X-Request-ID": "trace-500-abc"})
    assert r.status_code == 500 and r.headers["X-Request-ID"] == "trace-500-abc"
    error = next(rec for rec in caplog.records if rec.getMessage().startswith("Unhandled error"))
    assert error.request_id == "trace-500-abc" and "category=internal" in error.getMessage()
    (access,) = access_events(caplog.records)
    assert access.fields["status"] == 500 and access.fields["error_category"] == "internal"
    assert access.request_id == "trace-500-abc"
    assert "EXPLODESECRET42" not in all_text(caplog.records)


def test_access_log_can_be_disabled(test_settings, caplog):
    s = test_settings.model_copy(update={"access_log_enabled": False})
    with caplog.at_level(logging.INFO), TestClient(create_app(s)) as c:
        c.get("/health")
    assert access_events(caplog.records) == []


def test_every_layer_logs_with_the_same_request_id(client, caplog):
    with caplog.at_level(logging.INFO):
        r = client.post(CHAT, json={"query": "What is the capital of France?"}, headers={"X-Request-ID": "corr-xyz-1"})
    assert r.status_code == 200
    app_records = [rec for rec in caplog.records if rec.name.startswith("app.")]
    events = {getattr(rec, "event", None) for rec in app_records}
    assert {"chat.request_received", "chat.classified", "chat.model_selected", "provider.invoke",
            "provider.success", "chat.completed", "http.request"} <= events
    assert {rec.request_id for rec in app_records} == {"corr-xyz-1"}


# --------------------------------------------------------------------------- #
# End-to-end: nothing sensitive in ANY log at DEBUG level
# --------------------------------------------------------------------------- #
def test_no_sensitive_data_in_any_log_at_debug(caplog):
    secrets = {
        "openai_api_key": "sk-proj-E2EOPENAIKEY000001",
        "anthropic_api_key": "sk-ant-E2EANTHROPICKEY0002",
        "gemini_api_key": "AIzaE2EGEMINIKEY00000000003",
        "groq_api_key": "gsk_E2EGROQKEY00000000004",
        "mistral_api_key": "e2e-mistral-plain-key-0005",
        "admin_api_token": "e2e-admin-token-0006",
        "postgres_password": "e2e-pg-password-0007",
        "redis_password": "e2e-redis-password-0008",
        "client_id_hash_key": "e2e-hash-key-0009",
    }
    prompt = "My diary entry: the blue heron sings at midnight. What is the capital of France?"
    s = Settings(_env_file=None, llm_mock_mode=True, mock_failures={"mock-fast": "unavailable"},
                 provider_retry_backoff_seconds=0, **secrets)
    with caplog.at_level(logging.DEBUG), TestClient(create_app(s)) as c:
        r = c.post(CHAT, json={"query": prompt},
                   headers={"Authorization": "Bearer e2e-bearer-0010", "X-Forwarded-For": "198.51.100.77"})
        c.post(CHAT, json={"query": prompt, "api_key": "sk-proj-INJECTEDFIELD0011"})
        c.delete("/api/v1/cache", headers={"Authorization": "Bearer e2e-wrong-admin-0012"})
    configure_client_hashing(None)  # create_app configured the e2e key globally
    assert r.status_code == 200
    answer = r.json()["answer"]
    text = all_text(caplog.records)
    forbidden = [*secrets.values(), "e2e-bearer-0010", "sk-proj-INJECTEDFIELD0011", "e2e-wrong-admin-0012",
                 "blue heron", "diary entry", answer, "198.51.100.77", "testclient"]
    leaked = [value for value in forbidden if value in text]
    assert leaked == [], f"sensitive values found in logs: {leaked}"
    assert any(getattr(rec, "event", None) == "provider.failure" for rec in caplog.records)  # logs were captured


def test_settings_secret_values_cover_all_secrets():
    s = Settings(_env_file=None, openai_api_key="sk-a-12345", admin_api_token="adm-12345",
                 redis_password="rds-12345", client_id_hash_key="hk-12345")
    assert {"sk-a-12345", "adm-12345", "rds-12345", "hk-12345", "router_password"} <= set(s.secret_values())


# --------------------------------------------------------------------------- #
# Background persistence correlation (isolated test database)
# --------------------------------------------------------------------------- #
def test_persistence_logs_carry_request_ids(pg_clean, caplog):
    import asyncio

    from app.db.recorder import RequestRecord, RequestRecorder
    from app.infra.database import Database
    from tests.test_persistence import TEST_URL

    async def go():
        db = Database(TEST_URL)
        await db.start()
        rec = RequestRecorder(db)
        await rec.start()
        rec.record(RequestRecord(request_id="persist-rid-1", status="success", query_chars=1, latency_ms=1.0))
        await rec.flush()
        await rec.stop()
        await db.close()

    with caplog.at_level(logging.INFO, logger="app.db.recorder"):
        asyncio.run(go())
    persisted = next(r for r in caplog.records if getattr(r, "event", None) == "db.persisted")
    assert persisted.request_id == "persist-rid-1"
    assert persisted.fields["request_ids"] == "persist-rid-1" and persisted.fields["latency_ms"] >= 0
