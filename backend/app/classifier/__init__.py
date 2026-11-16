"""Rule-based query classification engine (no external calls, no API keys)."""

from app.classifier.engine import CLASSIFIER_VERSION, QueryClassifier
from app.classifier.types import ClassificationResult, ComplexityLevel, Intent, Thresholds

__all__ = [
    "CLASSIFIER_VERSION",
    "ClassificationResult",
    "ComplexityLevel",
    "Intent",
    "QueryClassifier",
    "Thresholds",
]
