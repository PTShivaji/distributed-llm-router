"""Core value types for the classification engine.

These are plain, immutable dataclasses so the engine has no dependency on
FastAPI or Pydantic and can be reused (e.g. by the router in Phase 3).
"""

from dataclasses import dataclass
from enum import StrEnum


class ComplexityLevel(StrEnum):
    SIMPLE = "SIMPLE"
    MEDIUM = "MEDIUM"
    COMPLEX = "COMPLEX"


class Intent(StrEnum):
    CONVERSATION = "conversation"
    QUESTION_ANSWERING = "question_answering"
    EXPLANATION = "explanation"
    CODE_GENERATION = "code_generation"
    DEBUGGING = "debugging"
    ANALYSIS = "analysis"
    SYSTEM_DESIGN = "system_design"
    PLANNING = "planning"
    MATH = "math"
    CREATIVE_WRITING = "creative_writing"
    SUMMARIZATION = "summarization"
    TRANSLATION = "translation"
    GENERAL = "general"


@dataclass(frozen=True, slots=True)
class Signal:
    """One piece of evidence that moved the complexity score."""

    name: str
    weight: float  # positive -> more complex, negative -> simpler
    detail: str


@dataclass(frozen=True, slots=True)
class QueryFeatures:
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


@dataclass(frozen=True, slots=True)
class IntentScore:
    intent: Intent
    score: float


@dataclass(frozen=True, slots=True)
class IntentResult:
    primary: Intent
    confidence: float
    alternatives: tuple[IntentScore, ...]


@dataclass(frozen=True, slots=True)
class TechnicalResult:
    is_technical: bool
    is_coding: bool
    score: float
    domains: tuple[str, ...]
    detected_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReasoningResult:
    score: float
    estimated_steps: int
    signals: tuple[Signal, ...]


@dataclass(frozen=True, slots=True)
class Thresholds:
    simple_max: float  # score <  simple_max  -> SIMPLE
    complex_min: float  # score >= complex_min -> COMPLEX

    def __post_init__(self) -> None:
        if not 0.0 < self.simple_max < self.complex_min < 1.0:
            raise ValueError("Thresholds must satisfy 0 < simple_max < complex_min < 1")


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    complexity: ComplexityLevel
    confidence: float
    complexity_score: float
    thresholds: Thresholds
    intent: IntentResult
    reasoning: ReasoningResult
    technical: TechnicalResult
    features: QueryFeatures
    explanation: str
    classifier_version: str
