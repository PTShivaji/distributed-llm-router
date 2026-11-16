"""Pydantic schemas for POST /api/v1/chat and GET /api/v1/models."""

from pydantic import BaseModel, ConfigDict, Field

from app.classifier.types import ComplexityLevel
from app.routing.registry import MODEL_ID_PATTERN
from app.schemas.classification import ClassifyRequest, ClassifyResponse


class ChatRequest(ClassifyRequest):
    """Inherits `query` validation (1..10,000 chars, not blank, no extra fields)."""

    query: str = Field(..., min_length=1, max_length=10_000, examples=["Explain Python variables"])
    model: str | None = Field(
        default=None, pattern=MODEL_ID_PATTERN, description="Optional override: a registered, enabled model id."
    )
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_output_tokens: int | None = Field(default=None, ge=1, le=32_000)
    use_cache: bool = Field(default=True, description="false = skip the response cache for this request.")


class _FromAttrs(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RoutingOut(_FromAttrs):
    selected_model: str
    requested_tier: ComplexityLevel
    routed_tier: ComplexityLevel
    strategy: str
    override: bool
    reason: str
    candidates: list[str]
    preferred_capabilities: list[str]


class AttemptOut(_FromAttrs):
    attempt_number: int
    model_id: str
    provider: str
    outcome: str
    error_kind: str | None
    retryable: bool | None
    latency_ms: float


class FallbackOut(_FromAttrs):
    used: bool
    original_model: str
    final_model: str
    chain: list[str]
    attempts: list[AttemptOut]
    reason: str | None


class UsageOut(_FromAttrs):
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    source: str = Field(description="provider_reported | estimated | partially_estimated | unavailable | cache_hit")


class CostOut(_FromAttrs):
    input_cost: float | None
    output_cost: float | None
    total_cost: float | None
    currency: str
    input_cost_per_million: float
    output_cost_per_million: float
    pricing_is_placeholder: bool
    is_estimate: bool = True


class CacheOut(_FromAttrs):
    status: str = Field(description="disabled | bypass | miss | hit_exact | hit_semantic | error")
    similarity: float | None = None
    threshold: float | None = None
    embedding_provider: str | None = None
    age_seconds: float | None = None
    source_request_id: str | None = None
    saved_tokens: int | None = None
    saved_cost: float | None = None
    stored: bool = False
    lookup_ms: float | None = None


class ChatResponse(_FromAttrs):
    request_id: str
    answer: str
    classification: ClassifyResponse
    selected_model: str
    model_used: str
    provider: str
    provider_adapter: str
    provider_model: str
    finish_reason: str | None
    routing: RoutingOut
    fallback: FallbackOut
    usage: UsageOut
    cost: CostOut
    latency_ms: float
    provider_status: str
    mock_mode: bool
    cache: CacheOut


class ModelHealthOut(_FromAttrs):
    state: str
    consecutive_failures: int
    total_successes: int
    total_failures: int
    last_error_kind: str | None


class ModelOut(BaseModel):
    """Public view of a registry entry (no api_base, no credentials)."""

    id: str
    provider: str
    provider_model: str
    tier: ComplexityLevel
    enabled: bool
    capabilities: list[str]
    priority: int
    input_cost_per_million: float
    output_cost_per_million: float
    pricing_is_placeholder: bool
    max_context_tokens: int
    max_output_tokens: int
    timeout_seconds: float
    fallbacks: list[str]
    description: str
    health: ModelHealthOut


class ModelsResponse(BaseModel):
    mock_mode: bool
    routing_strategy: str
    health_backend: str = Field(description="redis (shared across instances) | memory (process-local)")
    models: list[ModelOut]
