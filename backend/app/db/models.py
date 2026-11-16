"""SQLAlchemy 2.x ORM models.

request_log      one row per /chat request (success or provider failure):
                 classification, routing, model usage, tokens, cost, latency,
                 cache outcome. Raw prompt/answer columns stay NULL unless
                 PERSIST_PROMPTS / PERSIST_RESPONSES are enabled.
provider_attempt one row per provider attempt (retries, fallbacks, skips).
schema_migrations applied migration versions.

All timestamps are timezone-aware UTC.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RequestLog(Base):
    __tablename__ = "request_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(20))  # success | error
    error_code: Mapped[str | None] = mapped_column(String(64))
    client_hash: Mapped[str | None] = mapped_column(String(32))  # sha256 prefix, never the raw IP

    # Query metadata (no raw text by default)
    query_chars: Mapped[int] = mapped_column(Integer)
    prompt_text: Mapped[str | None] = mapped_column(Text)  # only if PERSIST_PROMPTS=true
    response_text: Mapped[str | None] = mapped_column(Text)  # only if PERSIST_RESPONSES=true

    # Classification
    complexity: Mapped[str | None] = mapped_column(String(10))
    complexity_score: Mapped[float | None] = mapped_column(Float)
    classification_confidence: Mapped[float | None] = mapped_column(Float)
    intent: Mapped[str | None] = mapped_column(String(40))
    is_coding: Mapped[bool | None] = mapped_column(Boolean)

    # Routing / model usage
    selected_model: Mapped[str | None] = mapped_column(String(100))
    model_used: Mapped[str | None] = mapped_column(String(100))
    provider: Mapped[str | None] = mapped_column(String(50))
    override: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    mock_mode: Mapped[bool] = mapped_column(Boolean, default=True)

    # Cache
    cache_status: Mapped[str] = mapped_column(String(20), default="disabled")
    cache_similarity: Mapped[float | None] = mapped_column(Float)

    # Tokens & cost (NULL = unavailable)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    usage_source: Mapped[str | None] = mapped_column(String(30))
    input_cost: Mapped[float | None] = mapped_column(Numeric(20, 10))
    output_cost: Mapped[float | None] = mapped_column(Numeric(20, 10))
    total_cost: Mapped[float | None] = mapped_column(Numeric(20, 10))
    pricing_is_placeholder: Mapped[bool | None] = mapped_column(Boolean)

    latency_ms: Mapped[float] = mapped_column(Float)

    attempts: Mapped[list["ProviderAttempt"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_request_log_created_at", "created_at"),
        Index("ix_request_log_model_used_created_at", "model_used", "created_at"),
        Index("ix_request_log_complexity_created_at", "complexity", "created_at"),
        Index("ix_request_log_cache_status_created_at", "cache_status", "created_at"),
        Index("ix_request_log_status_created_at", "status", "created_at"),
    )


class ProviderAttempt(Base):
    __tablename__ = "provider_attempt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_log_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("request_log.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempt_number: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    outcome: Mapped[str] = mapped_column(String(30))  # success | error | skipped_*
    error_kind: Mapped[str | None] = mapped_column(String(30))
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    latency_ms: Mapped[float] = mapped_column(Float)

    request: Mapped[RequestLog] = relationship(back_populates="attempts")

    __table_args__ = (
        Index("ix_provider_attempt_provider_outcome", "provider", "outcome"),
        Index("ix_provider_attempt_model_created_at", "model_id", "created_at"),
    )
