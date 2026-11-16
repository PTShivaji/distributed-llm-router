"""QueryClassifier: orchestrates the rule-based pipeline.

    text -> features -> technical -> intent -> reasoning score
         -> complexity level -> confidence -> explanation

Pure Python, deterministic, no I/O, no API keys. Safe to share across requests.
"""

from app.classifier.complexity import estimate_reasoning
from app.classifier.features import extract_features
from app.classifier.intent import detect_intent
from app.classifier.technical import detect_technical
from app.classifier.types import (
    ClassificationResult,
    ComplexityLevel,
    IntentResult,
    ReasoningResult,
    TechnicalResult,
    Thresholds,
)

CLASSIFIER_VERSION = "rule-based-1.0"

# Distance from a threshold at which we consider the level "fully certain".
CONFIDENCE_MARGIN_SATURATION = 0.15


class QueryClassifier:
    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.thresholds = thresholds or Thresholds(simple_max=0.28, complex_min=0.55)

    # ------------------------------------------------------------------ #
    def classify(self, query: str) -> ClassificationResult:
        text = query.strip()
        if not text:
            raise ValueError("query must not be empty")

        features = extract_features(text)
        technical = detect_technical(text, features)
        intent = detect_intent(text, features, technical)
        reasoning = estimate_reasoning(text, features, intent, technical)

        level = self._level_for(reasoning.score)
        margin = self._boundary_margin(reasoning.score, level)
        confidence = self._confidence(margin, intent)

        return ClassificationResult(
            complexity=level,
            confidence=confidence,
            complexity_score=reasoning.score,
            thresholds=self.thresholds,
            intent=intent,
            reasoning=reasoning,
            technical=technical,
            features=features,
            explanation=self._explain(level, reasoning, intent, technical, margin),
            classifier_version=CLASSIFIER_VERSION,
        )

    # ------------------------------------------------------------------ #
    def _level_for(self, score: float) -> ComplexityLevel:
        if score < self.thresholds.simple_max:
            return ComplexityLevel.SIMPLE
        if score < self.thresholds.complex_min:
            return ComplexityLevel.MEDIUM
        return ComplexityLevel.COMPLEX

    def _boundary_margin(self, score: float, level: ComplexityLevel) -> float:
        """0.0 = sitting on a threshold, 1.0 = comfortably inside its band."""
        t = self.thresholds
        match level:
            case ComplexityLevel.SIMPLE:
                distance = t.simple_max - score
            case ComplexityLevel.MEDIUM:
                distance = min(score - t.simple_max, t.complex_min - score)
            case ComplexityLevel.COMPLEX:
                distance = score - t.complex_min
        return min(distance / CONFIDENCE_MARGIN_SATURATION, 1.0)

    @staticmethod
    def _confidence(margin: float, intent: IntentResult) -> float:
        # Mostly driven by distance from the decision boundary; a clear
        # intent adds a little extra certainty. Range ~[0.55, 0.99].
        return round(min(0.50 + 0.35 * margin + 0.14 * intent.confidence, 0.99), 3)

    def _explain(
        self,
        level: ComplexityLevel,
        reasoning: ReasoningResult,
        intent: IntentResult,
        technical: TechnicalResult,
        margin: float,
    ) -> str:
        t = self.thresholds
        parts = [
            f"Classified as {level} (score {reasoning.score:.2f}; "
            f"SIMPLE < {t.simple_max:.2f} <= MEDIUM < {t.complex_min:.2f} <= COMPLEX)."
        ]
        parts.append(f"Primary intent: {intent.primary} (confidence {intent.confidence:.0%}).")

        top = sorted(reasoning.signals, key=lambda s: abs(s.weight), reverse=True)[:4]
        if top:
            factors = "; ".join(f"{s.detail} ({s.weight:+.2f})" for s in top)
            parts.append(f"Main factors: {factors}.")

        if technical.is_coding:
            parts.append(f"Coding query detected (domains: {', '.join(technical.domains)}).")
        elif technical.is_technical:
            parts.append(f"Technical query (domains: {', '.join(technical.domains)}).")
        else:
            parts.append("Non-technical query.")

        if margin < 0.34:
            parts.append("Confidence is reduced because the score is close to a level boundary.")
        return " ".join(parts)
