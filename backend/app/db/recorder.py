"""Background persistence of request history (never on the request's critical path).

`record_*` only enqueues (non-blocking). A single worker task drains the queue
in batches and writes them in a worker thread - persistence problems never fail
a user request.

Recovery (Phase 11.2, H2):
  - Records are queued even while PostgreSQL is unavailable (the queue is bounded
    by PERSISTENCE_QUEUE_SIZE). The worker waits for the database instead of
    writing into an outage, and drains the backlog once it reconnects.
  - A batch that fails with a connection-level error is retried with bounded
    exponential backoff (PERSISTENCE_WRITE_MAX_ATTEMPTS). If it still fails the
    database is reported unavailable (starting its reconnect loop) and the SAME
    batch is written after recovery - it is never discarded for a transient error.
  - No duplicates: every record carries a stable id; a batch that was attempted
    before is written idempotently (rows whose id already exists are skipped), so
    a commit that succeeded but whose acknowledgement was lost is not inserted twice.
  - Records are lost only when they cannot be kept: queue full, a permanent
    (non-connection) database error, or shutdown while the database is down. Each
    case is counted (dropped / failed), logged and exported as a metric.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.core.request_context import bind_request_id
from app.db.models import ProviderAttempt, RequestLog
from app.infra.database import DB_ERRORS, TRANSIENT_DB_ERRORS, Database
from app.observability.tracing import NOOP_TRACER, mark_error, set_attributes, start_span
from opentelemetry.trace import Link, Tracer

logger = logging.getLogger("app.db.recorder")
BATCH_SIZE = 50
MAX_LOGGED_REQUEST_IDS = 10


@dataclass
class RequestRecord:
    request_id: str
    status: str
    query_chars: int
    latency_ms: float
    client_hash: str | None = None
    error_code: str | None = None
    prompt_text: str | None = None
    response_text: str | None = None
    complexity: str | None = None
    complexity_score: float | None = None
    classification_confidence: float | None = None
    intent: str | None = None
    is_coding: bool | None = None
    selected_model: str | None = None
    model_used: str | None = None
    provider: str | None = None
    override: bool = False
    fallback_used: bool = False
    mock_mode: bool = True
    cache_status: str = "disabled"
    cache_similarity: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_source: str | None = None
    input_cost: float | None = None
    output_cost: float | None = None
    total_cost: float | None = None
    pricing_is_placeholder: bool | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Trace context of the originating request (never persisted): lets the
    # background db.persist_batch span LINK back to each request's trace.
    span_context: Any = field(default=None, repr=False, compare=False)
    # Stable primary key, fixed when the record is created (Phase 11.2): retries reuse it.
    record_id: uuid.UUID = field(default_factory=uuid.uuid4)


class Recorder(Protocol):
    persist_prompts: bool
    persist_responses: bool

    def record(self, record: RequestRecord) -> None: ...


class NullRecorder:
    persist_prompts = False
    persist_responses = False

    def record(self, record: RequestRecord) -> None:
        return None


def _write_batch(session: Session, batch: list[RequestRecord], *, idempotent: bool = False) -> tuple[int, int]:
    """Insert the batch in the caller's transaction. Returns (inserted, already_present).

    idempotent=True (a retry): rows whose id is already stored are skipped. A request row and
    its provider attempts are inserted in the same transaction, so an existing request row
    means its attempts exist too."""
    existing: set[uuid.UUID] = set()
    if idempotent:
        ids = [r.record_id for r in batch]
        existing = set(session.scalars(select(RequestLog.id).where(RequestLog.id.in_(ids))))
    inserted = 0
    for r in batch:
        if r.record_id in existing:
            continue
        attempts = r.attempts
        data = {k: v for k, v in r.__dict__.items() if k not in ("attempts", "span_context", "record_id")}
        row = RequestLog(id=r.record_id, attempt_count=len(attempts), **data)
        row.attempts = [ProviderAttempt(created_at=r.created_at, **a) for a in attempts]
        session.add(row)
        inserted += 1
    return inserted, len(existing)


class RequestRecorder:
    def __init__(
        self,
        database: Database,
        *,
        queue_size: int = 1000,
        persist_prompts: bool = False,
        persist_responses: bool = False,
        tracer: Tracer | None = None,
        write_max_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
        retry_backoff_max_seconds: float = 5.0,
    ) -> None:
        self.database = database
        self.tracer: Tracer = tracer or NOOP_TRACER
        self.persist_prompts = persist_prompts
        self.persist_responses = persist_responses
        self.write_max_attempts = max(1, write_max_attempts)
        self.retry_backoff_seconds = retry_backoff_seconds
        self.retry_backoff_max_seconds = retry_backoff_max_seconds
        self.metrics: Any = None  # set by the container (llm_router_db_* metrics)
        self._queue: asyncio.Queue[RequestRecord] | None = None
        self._queue_size = queue_size
        self._worker: asyncio.Task[None] | None = None
        self._inflight = 0  # records of the batch currently being written (for shutdown accounting)
        self.written = 0
        self.dropped = 0  # never queued (queue full / not started) or still pending at shutdown
        self.failed = 0  # permanent database errors (not retried)
        self.retries = 0  # write attempts repeated after a connection-level error
        self.deduplicated = 0  # rows skipped on retry because they were already stored

    def _count(self, outcome: str, n: int) -> None:
        if self.metrics is not None and n:
            self.metrics.db_persist_records(outcome, n)

    async def start(self) -> None:
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        self._worker = asyncio.create_task(self._run(), name="request-recorder")

    def record(self, record: RequestRecord) -> None:
        if self._queue is None:
            self.dropped += 1
            self._count("dropped_not_started", 1)
            log_event(logger, "db.persist_dropped", logging.WARNING, reason="not_started", dropped=self.dropped)
            return
        try:
            # Queued even while the database is unavailable: the worker waits for recovery.
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped += 1
            self._count("dropped_queue_full", 1)
            log_event(logger, "db.persist_dropped", logging.WARNING, reason="queue_full", dropped=self.dropped,
                      database_available=self.database.available)

    @property
    def pending(self) -> int:
        return (self._queue.qsize() if self._queue is not None else 0) + self._inflight

    async def flush(self) -> None:
        """Wait until everything queued is written. Returns early while the database is
        unavailable (nothing can be written then; the records stay queued for recovery)."""
        if self._queue is None:
            return
        join = asyncio.ensure_future(self._queue.join())
        try:
            while not join.done():
                if not self.database.available:
                    return
                await asyncio.wait({join}, timeout=0.05)
        finally:
            if not join.done():
                join.cancel()

    async def stop(self, timeout: float = 5.0) -> None:
        if self._queue is None:
            return
        try:
            await asyncio.wait_for(self.flush(), timeout)
        except TimeoutError:
            log_event(logger, "db.persist_flush_timeout", logging.WARNING, pending=self._queue.qsize())
        lost = self.pending
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        if lost:
            self.dropped += lost
            self._count("dropped_shutdown", lost)
            log_event(logger, "db.persist_dropped", logging.ERROR, reason="shutdown", rows=lost,
                      database_available=self.database.available)
        self._queue = self._worker = None
        self._inflight = 0

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            batch = [await self._queue.get()]
            while len(batch) < BATCH_SIZE and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            self._inflight = len(batch)
            # The worker runs outside any request; correlate its logs explicitly.
            # Single-record batch -> log line carries that request's rid; always list the ids.
            request_ids = ",".join(r.request_id for r in batch[:MAX_LOGGED_REQUEST_IDS])
            if len(batch) > MAX_LOGGED_REQUEST_IDS:
                request_ids += f",+{len(batch) - MAX_LOGGED_REQUEST_IDS}"
            with bind_request_id(batch[0].request_id if len(batch) == 1 else None):
                await self._write(batch, request_ids)
            self._inflight = 0
            for _ in batch:
                self._queue.task_done()

    async def _write(self, batch: list[RequestRecord], request_ids: str) -> None:
        links = [Link(r.span_context) for r in batch if r.span_context is not None]
        with start_span(self.tracer, "db.persist_batch", links=links, **{
            "db.system": "postgresql", "db.rows": len(batch),
            "db.queue_depth": self._queue.qsize() if self._queue else 0,
        }) as span:
            await self._write_traced(batch, request_ids, span)

    def _insert(self, session: Session, batch: list[RequestRecord], idempotent: bool) -> tuple[int, int]:
        # Runs in a worker thread; asyncio.to_thread copied the context, so this
        # span is a child of db.persist_batch.
        with start_span(self.tracer, "db.insert_batch", **{"db.system": "postgresql", "db.operation": "INSERT",
                                                         "db.rows": len(batch)}):
            return _write_batch(session, batch, idempotent=idempotent)

    def _backoff(self, attempt: int) -> float:
        return min(self.retry_backoff_seconds * (2 ** (attempt - 1)), self.retry_backoff_max_seconds)

    async def _write_traced(self, batch: list[RequestRecord], request_ids: str, span) -> None:
        started = time.perf_counter()
        attempted = False  # once a write was sent, later attempts must be idempotent
        failures = 0  # consecutive connection-level failures since the last (re)connect
        while True:
            if not self.database.available:
                log_event(logger, "db.persist_waiting", logging.WARNING, rows=len(batch), request_ids=request_ids)
                await self.database.wait_available()
            try:
                idempotent = attempted
                attempted = True
                inserted, existing = await self.database.run(lambda s: self._insert(s, batch, idempotent))
            except TRANSIENT_DB_ERRORS as exc:
                failures += 1
                self.retries += 1
                if self.metrics is not None:
                    self.metrics.db_write_retry()
                if failures >= self.write_max_attempts:
                    # Give the batch to the reconnect loop: wait for recovery, then write it (idempotently).
                    log_event(logger, "db.persist_retry_exhausted", logging.ERROR, rows=len(batch),
                              attempts=failures, error=type(exc).__name__, request_ids=request_ids)
                    self.database.mark_unavailable(f"write_failed:{type(exc).__name__}")
                    failures = 0
                    continue
                delay = self._backoff(failures)
                log_event(logger, "db.persist_retry", logging.WARNING, rows=len(batch), attempt=failures,
                          error=type(exc).__name__, retry_in_s=delay, request_ids=request_ids)
                await asyncio.sleep(delay)
                continue
            except DB_ERRORS as exc:  # permanent: retrying the same rows cannot succeed
                self.failed += len(batch)
                self._count("failed", len(batch))
                mark_error(span, type(exc).__name__, **{"db.outcome": "failed"})
                log_event(logger, "db.persist_failed", logging.ERROR, rows=len(batch), error=type(exc).__name__,
                          request_ids=request_ids, permanent=True)
                return
            except Exception:  # unexpected bug: log with traceback, keep the worker alive
                self.failed += len(batch)
                self._count("failed", len(batch))
                mark_error(span, "unexpected", **{"db.outcome": "failed"})
                logger.exception("db.persist_unexpected_error rows=%d request_ids=%s", len(batch), request_ids)
                return
            set_attributes(span, **{"db.outcome": "written"})
            self.written += inserted
            self.deduplicated += existing
            self._count("written", inserted)
            self._count("deduplicated", existing)
            log_event(logger, "db.persisted", rows=inserted, total_written=self.written,
                      request_ids=request_ids, latency_ms=round((time.perf_counter() - started) * 1000, 2),
                      **({"already_stored": existing} if existing else {}))
            return
