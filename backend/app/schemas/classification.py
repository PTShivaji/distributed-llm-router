"""Pydantic request/response schemas for POST /api/v1/classify.

Response models read directly from the engine's dataclasses
(`from_attributes=True`), so the API layer has no mapping code to drift.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.classifier.types import ComplexityLevel, Intent

MAX_QUERY_LENGTH = 10_000


class ClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUERY_LENGTH,
        description="The user query to classify.",
        examples=["Design a fault-tolerant distributed cache and compare consistency trade-offs."],
    )

    @field_validator("query")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v


class _FromAttrs(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SignalOut(_FromAttrs):
    name: str
    weight: float = Field(description="Contribution to the complexity score (negative = simpler).")
    detail: str


class IntentScoreOut(_FromAttrs):
    intent: Intent
    score: float


class IntentOut(_FromAttrs):
    primary: Intent
    confidence: float = Field(ge=0, le=1)
    alternatives: list[IntentScoreOut]


class ReasoningOut(_FromAttrs):
    score: float = Field(ge=0, le=1)
    estimated_steps: int = Field(ge=1, description="Rough number of reasoning steps required.")
    signals: list[SignalOut]


class TechnicalOut(_FromAttrs):
    is_technical: bool
    is_coding: bool
    score: float = Field(ge=0, le=1)
    domains: list[str]
    detected_terms: list[str]


class FeaturesOut(_FromAttrs):
    word_count: int
    char_count: int
    sentence_count: int
    question_count: int
    line_count: int
    list_item_count: int
    has_code_block: bool
    code_line_count: int
    inline_code_count: int
    has_code: bool


class ThresholdsOut(_FromAttrs):
    simple_max: float
    complex_min: float


class ClassifyResponse(_FromAttrs):
    complexity: ComplexityLevel
    confidence: float = Field(ge=0, le=1, description="Certainty in the complexity level.")
    complexity_score: float = Field(ge=0, le=1)
    thresholds: ThresholdsOut
    intent: IntentOut
    reasoning: ReasoningOut
    technical: TechnicalOut
    features: FeaturesOut
    explanation: str
    classifier_version: str
