"""Intent detection via weighted pattern scoring."""

from app.classifier.lexicons import INTENT_PATTERNS, LEADING_VERB_BONUS, LEADING_VERBS
from app.classifier.types import Intent, IntentResult, IntentScore, QueryFeatures, TechnicalResult

_PRIORITY = {intent: i for i, intent in enumerate(INTENT_PATTERNS)}


def _score_intents(text: str, features: QueryFeatures, technical: TechnicalResult) -> dict[Intent, float]:
    scores = {
        intent: sum(weight for regex, weight in patterns if regex.search(text))
        for intent, patterns in INTENT_PATTERNS.items()
    }
    for intent, regex in LEADING_VERBS.items():
        if regex.search(text):
            scores[intent] += LEADING_VERB_BONUS
            break  # only one leading verb can exist
    # Contextual adjustments
    if features.has_code:
        if scores[Intent.DEBUGGING] > 0:
            scores[Intent.DEBUGGING] += 1.0  # code + error language => debugging
        elif scores[Intent.EXPLANATION] == 0:
            scores[Intent.CODE_GENERATION] += 0.5
    if technical.is_technical and scores[Intent.QUESTION_ANSWERING] > 0 and scores[Intent.EXPLANATION] > 0:
        scores[Intent.EXPLANATION] += 0.3  # technical "how/why" leans to explanation
    return scores


def detect_intent(text: str, features: QueryFeatures, technical: TechnicalResult) -> IntentResult:
    scores = _score_intents(text, features, technical)
    ranked = sorted(
        ((i, s) for i, s in scores.items() if s > 0),
        key=lambda item: (-item[1], _PRIORITY[item[0]]),
    )

    if not ranked:
        return IntentResult(primary=Intent.GENERAL, confidence=0.4, alternatives=())

    top_intent, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    # Confidence = how clearly the top intent beats the runner-up,
    # tempered by how much evidence there is in absolute terms.
    separation = (top - second) / top
    evidence = min(top / 2.0, 1.0)
    confidence = 0.45 + 0.35 * separation + 0.19 * evidence

    return IntentResult(
        primary=top_intent,
        confidence=round(min(confidence, 0.99), 3),
        alternatives=tuple(IntentScore(i, round(s, 3)) for i, s in ranked[1:4]),
    )
