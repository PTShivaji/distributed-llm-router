"""Routing policy: tier selection, overrides, strategies, health, fallback chains."""

import pytest

from app.classifier import ComplexityLevel, QueryClassifier
from app.routing.errors import ModelDisabledError, NoModelAvailableError, UnknownModelError
from app.routing.health import HealthTracker
from app.routing.policy import RoutingPolicy, tier_search_order
from app.routing.registry import ModelRegistry, load_registry
from tests.conftest import model

SIMPLE_Q = "What is the capital of France?"
MEDIUM_Q = "Compare REST and GraphQL for a mobile app backend."
COMPLEX_Q = (
    "Design a distributed rate limiter for a multi-region API gateway that must handle 1M requests "
    "per second. Compare token bucket and sliding window approaches and analyze the trade-offs."
)
CODING_MEDIUM_Q = "Write a Python function that parses a CSV file and returns the average of each column, handling missing values."

C = QueryClassifier()


def policy(models, **kw) -> RoutingPolicy:
    return RoutingPolicy(ModelRegistry(models), kw.pop("health", HealthTracker()), **kw)


# --------------------------------------------------------------------------- #
# Tier routing with the real Phase 2 classifier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query, tier, model_id",
    [(SIMPLE_Q, "SIMPLE", "fast"), (MEDIUM_Q, "MEDIUM", "balanced"), (COMPLEX_Q, "COMPLEX", "advanced")],
)
def test_each_tier_routes_to_its_model(three_tier_models, query, tier, model_id):
    cls = C.classify(query)
    d = policy(three_tier_models).select(cls)
    assert cls.complexity == tier
    assert d.model.id == model_id
    assert d.requested_tier == d.routed_tier == tier
    assert not d.override


@pytest.mark.parametrize(
    "query, expected",
    [(SIMPLE_Q, "mock-fast"), (MEDIUM_Q, "mock-balanced"), (CODING_MEDIUM_Q, "mock-coder"), (COMPLEX_Q, "mock-advanced")],
)
def test_default_registry_routing(query, expected):
    d = RoutingPolicy(load_registry(None), HealthTracker()).select(C.classify(query))
    assert d.model.id == expected


def test_reason_explains_selection(three_tier_models):
    d = policy(three_tier_models).select(C.classify(COMPLEX_Q))
    assert "classified as COMPLEX" in d.reason
    assert "'advanced'" in d.reason and "priority" in d.reason
    assert "reasoning" in d.preferred_capabilities


def test_coding_capability_preferred():
    models = [model("general", "MEDIUM", priority=1), model("coder", "MEDIUM", priority=50, capabilities=("chat", "coding"))]
    p = policy(models)
    assert p.select(C.classify(CODING_MEDIUM_Q)).model.id == "coder"
    assert p.select(C.classify(MEDIUM_Q)).model.id == "general"


def test_lowest_cost_strategy():
    models = [
        model("pricey", "SIMPLE", priority=1, input_cost_per_million=5, output_cost_per_million=5),
        model("cheap", "SIMPLE", priority=99, input_cost_per_million=0.1, output_cost_per_million=0.1),
    ]
    assert policy(models, strategy="priority").select(C.classify(SIMPLE_Q)).model.id == "pricey"
    assert policy(models, strategy="lowest_cost").select(C.classify(SIMPLE_Q)).model.id == "cheap"


# --------------------------------------------------------------------------- #
# Disabled models, escalation, no model
# --------------------------------------------------------------------------- #
def test_disabled_model_never_selected():
    models = [model("off", "SIMPLE", enabled=False, priority=0), model("on", "SIMPLE", priority=50)]
    d = policy(models).select(C.classify(SIMPLE_Q))
    assert d.model.id == "on" and "off" not in d.candidates


def test_escalates_to_higher_tier_when_tier_empty():
    models = [model("fast", "SIMPLE", enabled=False), model("balanced", "MEDIUM"), model("advanced", "COMPLEX")]
    d = policy(models).select(C.classify(SIMPLE_Q))
    assert d.model.id == "balanced"
    assert d.requested_tier == ComplexityLevel.SIMPLE and d.routed_tier == ComplexityLevel.MEDIUM
    assert "Routed to MEDIUM tier instead of SIMPLE" in d.reason


def test_complex_tier_empty_falls_to_lower_tier():
    d = policy([model("balanced", "MEDIUM")]).select(C.classify(COMPLEX_Q))
    assert d.model.id == "balanced" and d.routed_tier == ComplexityLevel.MEDIUM


def test_no_escalation_raises():
    with pytest.raises(NoModelAvailableError):
        policy([model("balanced", "MEDIUM")], tier_escalation=False).select(C.classify(SIMPLE_Q))


def test_all_disabled_raises():
    with pytest.raises(NoModelAvailableError):
        policy([model("a", enabled=False)]).select(C.classify(SIMPLE_Q))


def test_tier_search_order():
    S, M, X = ComplexityLevel.SIMPLE, ComplexityLevel.MEDIUM, ComplexityLevel.COMPLEX
    assert tier_search_order(S, True) == [S, M, X]
    assert tier_search_order(M, True) == [M, X, S]
    assert tier_search_order(X, True) == [X, M, S]
    assert tier_search_order(M, False) == [M]


def test_context_window_filter():
    models = [model("small", "SIMPLE", max_context_tokens=1000, max_output_tokens=500), model("big", "MEDIUM", max_context_tokens=100_000)]
    d = policy(models).select(C.classify(SIMPLE_Q), estimated_input_tokens=900, max_output_tokens=500)
    assert d.model.id == "big"


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #
def test_valid_override(three_tier_models):
    d = policy(three_tier_models).select(C.classify(SIMPLE_Q), override="advanced")
    assert d.model.id == "advanced" and d.override and d.strategy == "override"
    assert d.requested_tier == ComplexityLevel.SIMPLE and d.routed_tier == ComplexityLevel.COMPLEX
    assert "Explicit override" in d.reason


def test_unknown_override_rejected(three_tier_models):
    with pytest.raises(UnknownModelError):
        policy(three_tier_models).select(C.classify(SIMPLE_Q), override="gpt-imaginary")


def test_disabled_override_rejected():
    with pytest.raises(ModelDisabledError):
        policy([model("on"), model("off", enabled=False)]).select(C.classify(SIMPLE_Q), override="off")


# --------------------------------------------------------------------------- #
# Health-aware selection
# --------------------------------------------------------------------------- #
def test_unhealthy_model_skipped_in_selection():
    health = HealthTracker(failure_threshold=1, cooldown_seconds=60)
    models = [model("primary", "SIMPLE", priority=1), model("backup", "SIMPLE", priority=2)]
    health.record_failure("primary", "unavailable")
    d = policy(models, health=health).select(C.classify(SIMPLE_Q))
    assert d.model.id == "backup"


def test_all_unhealthy_still_tries_best():
    health = HealthTracker(failure_threshold=1, cooldown_seconds=60)
    health.record_failure("only", "unavailable")
    d = policy([model("only")], health=health).select(C.classify(SIMPLE_Q))
    assert d.model.id == "only" and "unhealthy" in d.reason


# --------------------------------------------------------------------------- #
# Fallback chain
# --------------------------------------------------------------------------- #
def test_fallback_chain_configured_first_then_auto(three_tier_models):
    p = policy(three_tier_models)
    primary = p.registry.get("fast")
    chain = [m.id for m in p.fallback_chain(primary, C.classify(SIMPLE_Q))]
    assert chain == ["balanced", "advanced"]


def test_fallback_chain_never_contains_primary_or_duplicates():
    # Mutual fallbacks (a <-> b) would loop forever if the chain were not de-duplicated.
    models = [model("a", fallbacks=("b",)), model("b", fallbacks=("a",)), model("c")]
    p = policy(models)
    chain = [m.id for m in p.fallback_chain(p.registry.get("a"), C.classify(SIMPLE_Q))]
    assert chain == ["b", "c"]
    assert len(chain) == len(set(chain))


def test_fallback_chain_respects_cap_and_disabled():
    models = [model("p", fallbacks=("x", "y", "z")), model("x", enabled=False), model("y"), model("z"), model("w")]
    p = policy(models, max_fallback_models=2)
    assert [m.id for m in p.fallback_chain(p.registry.get("p"), C.classify(SIMPLE_Q))] == ["y", "z"]


def test_fallback_chain_without_auto_tier(three_tier_models):
    p = policy(three_tier_models, fallback_auto_tier=False)
    assert [m.id for m in p.fallback_chain(p.registry.get("fast"), C.classify(SIMPLE_Q))] == ["balanced"]


def test_zero_fallbacks():
    p = policy([model("a"), model("b")], max_fallback_models=0)
    assert p.fallback_chain(p.registry.get("a"), C.classify(SIMPLE_Q)) == []
