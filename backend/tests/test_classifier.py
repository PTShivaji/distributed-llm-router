"""Unit tests for the rule-based classification engine (no HTTP)."""

import pytest

from app.classifier import ComplexityLevel, Intent, QueryClassifier, Thresholds
from app.classifier.features import extract_features
from app.classifier.technical import detect_technical

S, M, C = ComplexityLevel.SIMPLE, ComplexityLevel.MEDIUM, ComplexityLevel.COMPLEX

DESIGN_QUERY = (
    "Design a distributed rate limiter for a multi-region API gateway that must handle "
    "1M requests per second, guarantee fairness across tenants, and remain fault-tolerant "
    "during network partitions. Compare token bucket and sliding window approaches and "
    "analyze the trade-offs."
)
KEYERROR_QUERY = "Why does my code throw a KeyError?\n```python\nd = {}\nprint(d['x'])\n```"


# --------------------------------------------------------------------------- #
# Complexity levels
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query, expected",
    [
        ("Hello!", S),
        ("What is the capital of France?", S),
        ("Who wrote Hamlet?", S),
        ("Define photosynthesis.", S),
        ("Translate 'good morning' into Spanish.", S),
        ("What is 15 * 24?", S),
        ("Write a haiku about autumn.", S),
        ("Summarize the plot of Romeo and Juliet.", S),
        ("Explain how a hash map works.", S),
        ("Write a Python function that parses a CSV file and returns the average of each column, handling missing values.", M),
        (KEYERROR_QUERY, M),
        ("Compare REST and GraphQL for a mobile app backend.", M),
        ("What are the pros and cons of microservices versus a monolith?", M),
        ("Explain the difference between processes and threads in Python and when to use each.", M),
        ("Prove that the square root of 2 is irrational, step by step.", M),
        (DESIGN_QUERY, C),
        (
            "Architect a scalable, fault-tolerant event-driven microservices platform on Kubernetes "
            "with Kafka, explain consistency trade-offs, and outline a migration plan from our monolith.",
            C,
        ),
        (
            "Analyze the root cause of this deadlock in our multithreading code and propose an optimal "
            "fix that ensures no race condition:\n```java\nsynchronized(a){ synchronized(b){ work(); } }\n```",
            C,
        ),
    ],
)
def test_complexity_level(classifier, query, expected):
    assert classifier.classify(query).complexity == expected


def test_complexity_is_monotonic_with_added_requirements(classifier):
    base = classifier.classify("Write a Python function to sort a list.").complexity_score
    richer = classifier.classify(
        "Write a Python function to sort a list. It must be stable, handle None values, "
        "support custom keys, and be optimized for performance on large inputs. "
        "Also explain the time complexity trade-offs."
    ).complexity_score
    assert richer > base


def test_score_is_bounded(classifier):
    huge = (DESIGN_QUERY + " ") * 30
    r = classifier.classify(huge)
    assert 0.0 <= r.complexity_score <= 1.0
    assert r.complexity == C


# --------------------------------------------------------------------------- #
# Intent detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query, intent",
    [
        ("Hi there", Intent.CONVERSATION),
        ("What is the capital of Japan?", Intent.QUESTION_ANSWERING),
        ("Explain how TCP handshakes work", Intent.EXPLANATION),
        ("Write a Python function to reverse a string", Intent.CODE_GENERATION),
        (KEYERROR_QUERY, Intent.DEBUGGING),
        ("Fix this bug: my loop never terminates", Intent.DEBUGGING),
        ("Compare Postgres and MongoDB for analytics", Intent.ANALYSIS),
        (DESIGN_QUERY, Intent.SYSTEM_DESIGN),
        ("Create a 3-month roadmap to learn machine learning", Intent.PLANNING),
        ("Solve the equation 2x + 3 = 11", Intent.MATH),
        ("Write a poem about the ocean", Intent.CREATIVE_WRITING),
        ("Summarize this article in three bullet points", Intent.SUMMARIZATION),
        ("Translate this sentence into French", Intent.TRANSLATION),
        ("Tell me a fun fact.", Intent.GENERAL),
    ],
)
def test_intent_detection(classifier, query, intent):
    assert classifier.classify(query).intent.primary == intent


def test_leading_verb_decides_mixed_intent(classifier):
    r = classifier.classify(DESIGN_QUERY)
    assert r.intent.primary == Intent.SYSTEM_DESIGN
    assert Intent.ANALYSIS in {a.intent for a in r.intent.alternatives}


def test_unknown_intent_has_low_confidence(classifier):
    assert classifier.classify("Tell me a fun fact.").intent.confidence < 0.5


def test_clear_intent_has_high_confidence(classifier):
    assert classifier.classify("Translate this into German").intent.confidence > 0.9


# --------------------------------------------------------------------------- #
# Technical & coding detection
# --------------------------------------------------------------------------- #
def test_non_technical_query(classifier):
    t = classifier.classify("Write a poem about the ocean").technical
    assert not t.is_technical and not t.is_coding
    assert t.domains == ()


def test_technical_but_not_coding(classifier):
    t = classifier.classify("What is the CAP theorem in distributed systems?").technical
    assert t.is_technical and not t.is_coding
    assert "systems" in t.domains
    assert "cap theorem" in t.detected_terms


def test_coding_request_without_code(classifier):
    t = classifier.classify("Implement a binary search function in Rust").technical
    assert t.is_coding
    assert {"programming", "algorithms"} <= set(t.domains)


def test_code_snippet_detected_as_coding():
    text = "Can you look at this?\ndef add(a, b):\n    return a + b\n"
    f = extract_features(text)
    assert f.has_code and not f.has_code_block
    assert detect_technical(text, f).is_coding


def test_fenced_code_block_detected():
    f = extract_features("Review:\n```js\nconst x = 1;\n```")
    assert f.has_code_block and f.has_code


def test_traceback_detected_as_code():
    text = 'Traceback (most recent call last):\n  File "app.py", line 3, in <module>\nKeyError: \'x\''
    assert extract_features(text).has_code


def test_term_matching_respects_word_boundaries(classifier):
    # "rapid" contains "api", "restaurant" contains "rest" -> must not match
    t = classifier.classify("Find a rapid restaurant recommendation").technical
    assert not t.is_technical


@pytest.mark.parametrize("term", ["C++", "node.js", "CI/CD"])
def test_special_character_terms(classifier, term):
    assert classifier.classify(f"Tell me about {term}").technical.is_technical


def test_advanced_terms_raise_technical_score(classifier):
    basic = classifier.classify("What is HTML?").technical.score
    advanced = classifier.classify("How does Raft consensus handle replication?").technical.score
    assert advanced > basic


# --------------------------------------------------------------------------- #
# Features
# --------------------------------------------------------------------------- #
def test_feature_counts():
    f = extract_features("First question? Second question?\n- item one\n- item two\n")
    assert f.question_count == 2
    assert f.list_item_count == 2
    assert f.line_count == 3
    assert f.word_count == 8


# --------------------------------------------------------------------------- #
# Reasoning estimation
# --------------------------------------------------------------------------- #
def test_reasoning_signals_explain_the_score(classifier):
    r = classifier.classify(DESIGN_QUERY)
    names = {s.name for s in r.reasoning.signals}
    assert {"intent_prior", "reasoning_marker", "technical_depth", "constraints"} <= names
    # Score equals the (clamped) sum of its signals -> fully traceable
    assert r.complexity_score == pytest.approx(min(sum(s.weight for s in r.reasoning.signals), 1.0), abs=1e-3)


def test_simplicity_markers_lower_score(classifier):
    r = classifier.classify("What is Python? Just a short answer.")
    assert any(s.weight < 0 for s in r.reasoning.signals)


def test_estimated_steps_grow_with_complexity(classifier):
    simple = classifier.classify("What is Redis?").reasoning.estimated_steps
    complex_ = classifier.classify(DESIGN_QUERY).reasoning.estimated_steps
    assert simple == 1
    assert complex_ > simple


# --------------------------------------------------------------------------- #
# Confidence scoring
# --------------------------------------------------------------------------- #
def test_confidence_in_range(classifier):
    for q in ["Hi", "Explain recursion", DESIGN_QUERY, KEYERROR_QUERY]:
        assert 0.5 <= classifier.classify(q).confidence <= 0.99


def test_confidence_lower_near_boundary():
    # Same query, thresholds moved so its score sits right next to a boundary.
    query = "Compare REST and GraphQL for a mobile app backend."
    score = QueryClassifier().classify(query).complexity_score

    far = QueryClassifier(Thresholds(simple_max=0.05, complex_min=0.95)).classify(query)
    near = QueryClassifier(Thresholds(simple_max=score - 0.005, complex_min=0.95)).classify(query)

    assert far.complexity == near.complexity == M
    assert near.confidence < far.confidence
    assert "close to a level boundary" in near.explanation


def test_clear_cases_are_highly_confident(classifier):
    assert classifier.classify("Hello!").confidence >= 0.9
    assert classifier.classify(DESIGN_QUERY).confidence >= 0.9


# --------------------------------------------------------------------------- #
# Explanation
# --------------------------------------------------------------------------- #
def test_explanation_mentions_level_intent_and_factors(classifier):
    r = classifier.classify(DESIGN_QUERY)
    assert r.explanation.startswith("Classified as COMPLEX")
    assert "system_design" in r.explanation
    assert "Main factors:" in r.explanation
    assert "trade-off analysis" in r.explanation


def test_explanation_flags_non_technical(classifier):
    assert "Non-technical query." in classifier.classify("Write a poem about rain").explanation


# --------------------------------------------------------------------------- #
# Thresholds & robustness
# --------------------------------------------------------------------------- #
def test_custom_thresholds_change_level():
    query = "Compare REST and GraphQL for a mobile app backend."
    assert QueryClassifier(Thresholds(0.1, 0.2)).classify(query).complexity == C
    assert QueryClassifier(Thresholds(0.8, 0.9)).classify(query).complexity == S


@pytest.mark.parametrize("bad", [(0.5, 0.5), (0.6, 0.4), (0.0, 0.5), (0.3, 1.0)])
def test_invalid_thresholds_rejected(bad):
    with pytest.raises(ValueError):
        Thresholds(*bad)


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_blank_query_rejected(classifier, blank):
    with pytest.raises(ValueError):
        classifier.classify(blank)


def test_deterministic(classifier):
    assert classifier.classify(DESIGN_QUERY) == classifier.classify(DESIGN_QUERY)


@pytest.mark.parametrize("query", ["???", "12345", "🙂🙂🙂", "a" * 10_000, "<script>alert(1)</script>"])
def test_unusual_input_does_not_crash(classifier, query):
    r = classifier.classify(query)
    assert r.complexity in (S, M, C)
