"""Schemas for cache management, analytics and dependency status endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _FromAttrs(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class CacheStatsResponse(_FromAttrs):
    enabled: bool
    semantic_enabled: bool
    available: bool = Field(description="False when Redis is unreachable.")
    embedding_provider: str | None
    embedding_is_semantic: bool = Field(description="False for the local lexical hashing embedder.")
    similarity_threshold: float | None
    ttl_seconds: int | None
    total_entries: int | None
    scopes: int | None
    hits: int
    hits_exact: int
    hits_semantic: int
    misses: int
    stores: int
    hit_rate: float | None
    approx_storage_bytes: int | None
    notes: list[str]


class CacheClearResponse(BaseModel):
    cleared: bool
    deleted_keys: int
    reset_stats: bool


class ModelCount(BaseModel):
    model: str
    requests: int
    total_tokens: int
    estimated_cost: float
    average_latency_ms: float | None


class ProviderCount(BaseModel):
    provider: str
    successes: int
    failures: int
    skipped: int


class AnalyticsSummaryResponse(_FromAttrs):
    start: datetime | None
    end: datetime | None
    total_requests: int
    successful_requests: int
    failed_requests: int
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    estimated_total_cost: float = Field(description="Sum of estimated costs (placeholder pricing in mock mode).")
    average_latency_ms: float | None
    cache_hits: int
    cache_misses: int
    cache_hit_rate: float | None
    fallback_requests: int
    fallback_rate: float | None
    requests_by_complexity: dict[str, int]
    requests_by_model: list[ModelCount]
    provider_attempts: list[ProviderCount]


class DependencyStatus(BaseModel):
    enabled: bool
    reachable: bool


class DependenciesResponse(BaseModel):
    redis: DependencyStatus
    database: DependencyStatus
    cache_enabled: bool
    rate_limit_enabled: bool
    health_backend: str
    persistence: dict[str, int]
