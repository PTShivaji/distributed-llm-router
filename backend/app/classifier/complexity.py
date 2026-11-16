"""Reasoning complexity estimation.

Produces a score in [0, 1] as the sum of independent, capped signals so every
point of the score can be traced back to a reason (see `Signal`).
"""

from app.classifier.lexicons import (
    ADVANCED_TECH_TERMS,
    CONSTRAINT_CAP,
    CONSTRAINT_REGEX,
    CONSTRAINT_WEIGHT,
    INTENT_COMPLEXITY_PRIOR,
    REASONING_MARKER_CAP,
    REASONING_MARKERS,
    SEQUENCE_REGEX,
    SIMPLICITY_MARKERS,
)
from app.classifier.types import IntentResult, QueryFeatures, ReasoningResult, Signal, TechnicalResult

LENGTH_MAX_WEIGHT = 0.20
LENGTH_SATURATION_WORDS = 150
MULTIPART_CAP = 0.15
TECH_CAP = 0.20


def _length_signal(f: QueryFeatures) -> Signal | None:
    weight = min(f.word_count / LENGTH_SATURATION_WORDS, 1.0) * LENGTH_MAX_WEIGHT
    if weight < 0.01:
        return None
    return Signal("length", round(weight, 3), f"{f.word_count} words")


def _reasoning_signals(text: str) -> tuple[list[Signal], int]:
    signals, total, hits = [], 0.0, 0
    for regex, weight, label in REASONING_MARKERS:
        if regex.search(text):
            applied = min(weight, REASONING_MARKER_CAP - total)
            if applied <= 0:
                break
            total += applied
            hits += 1
            signals.append(Signal("reasoning_marker", round(applied, 3), label))
    return signals, hits


def _multipart_signal(text: str, f: QueryFeatures) -> tuple[Signal | None, int]:
    extra_questions = max(f.question_count - 1, 0)
    list_items = f.list_item_count if f.list_item_count >= 2 else 0
    sequence_words = len({m.group(0).lower() for m in SEQUENCE_REGEX.finditer(text)})
    parts = extra_questions + list_items + sequence_words
    if parts == 0:
        return None, 0
    weight = min(0.05 * extra_questions + 0.04 * list_items + 0.03 * sequence_words, MULTIPART_CAP)
    detail = f"{extra_questions} extra question(s), {list_items} list item(s), {sequence_words} sequencing word(s)"
    return Signal("multi_part", round(weight, 3), detail), parts


def _constraint_signal(text: str) -> Signal | None:
    found = {m.group(0).lower() for m in CONSTRAINT_REGEX.finditer(text)}
    if not found:
        return None
    weight = min(len(found) * CONSTRAINT_WEIGHT, CONSTRAINT_CAP)
    return Signal("constraints", round(weight, 3), f"constraint language: {', '.join(sorted(found))}")


def _technical_signals(f: QueryFeatures, t: TechnicalResult) -> list[Signal]:
    signals = []
    if t.detected_terms:
        advanced = [x for x in t.detected_terms if x in ADVANCED_TECH_TERMS]
        weight = min(0.03 * len(t.detected_terms) + 0.04 * len(advanced), TECH_CAP)
        detail = f"technical terms: {', '.join(t.detected_terms[:8])}"
        if advanced:
            detail += f" (advanced: {', '.join(advanced[:5])})"
        signals.append(Signal("technical_depth", round(weight, 3), detail))
    if f.has_code:
        weight = 0.10 + (0.05 if f.code_line_count > 20 else 0.0)
        signals.append(Signal("code_present", weight, f"{f.code_line_count} line(s) of code included"))
    return signals


def estimate_reasoning(
    text: str, features: QueryFeatures, intent: IntentResult, technical: TechnicalResult
) -> ReasoningResult:
    signals: list[Signal] = []

    prior = INTENT_COMPLEXITY_PRIOR[intent.primary]
    if prior:
        signals.append(Signal("intent_prior", prior, f"baseline demand of '{intent.primary}' intent"))

    if s := _length_signal(features):
        signals.append(s)

    marker_signals, marker_hits = _reasoning_signals(text)
    signals.extend(marker_signals)

    multipart, parts = _multipart_signal(text, features)
    if multipart:
        signals.append(multipart)

    if s := _constraint_signal(text):
        signals.append(s)

    signals.extend(_technical_signals(features, technical))

    for regex, weight, label in SIMPLICITY_MARKERS:
        if regex.search(text):
            signals.append(Signal("simplicity_marker", weight, label))

    score = min(max(sum(s.weight for s in signals), 0.0), 1.0)
    estimated_steps = 1 + marker_hits + min(parts, 5)

    return ReasoningResult(score=round(score, 3), estimated_steps=estimated_steps, signals=tuple(signals))
