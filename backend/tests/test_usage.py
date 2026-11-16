"""Token usage and cost calculation."""

import pytest

from app.routing.usage import TokenUsage, build_usage, estimate_cost, estimate_tokens
from tests.conftest import model


def test_estimate_tokens_heuristic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_provider_reported_usage_used_verbatim():
    u = build_usage(reported_input=120, reported_output=30, prompt_text="x" * 999, completion_text="y", estimate_missing=True)
    assert u == TokenUsage(120, 30, 150, "provider_reported")


def test_missing_usage_is_estimated_and_labelled():
    u = build_usage(reported_input=None, reported_output=None, prompt_text="a" * 40, completion_text="b" * 8, estimate_missing=True)
    assert u == TokenUsage(10, 2, 12, "estimated")


def test_partial_usage_is_labelled_partially_estimated():
    u = build_usage(reported_input=50, reported_output=None, prompt_text="ignored", completion_text="b" * 8, estimate_missing=True)
    assert u == TokenUsage(50, 2, 52, "partially_estimated")


def test_missing_usage_unavailable_when_estimation_disabled():
    u = build_usage(reported_input=None, reported_output=None, prompt_text="a", completion_text="b", estimate_missing=False)
    assert u == TokenUsage(None, None, None, "unavailable")


def test_cost_calculation():
    m = model("m", input_cost_per_million=2.0, output_cost_per_million=8.0)
    c = estimate_cost(TokenUsage(1_000_000, 500_000, 1_500_000, "provider_reported"), m)
    assert (c.input_cost, c.output_cost, c.total_cost) == (2.0, 4.0, 6.0)
    assert c.currency == "USD" and c.pricing_is_placeholder


def test_small_costs_keep_precision():
    m = model("m", input_cost_per_million=0.1, output_cost_per_million=0.4)
    c = estimate_cost(TokenUsage(6, 50, 56, "estimated"), m)
    assert c.input_cost == pytest.approx(6e-7)
    assert c.total_cost == pytest.approx(2.06e-5)


def test_cost_unavailable_when_tokens_unavailable():
    c = estimate_cost(TokenUsage(None, None, None, "unavailable"), model("m"))
    assert c.input_cost is None and c.output_cost is None and c.total_cost is None


def test_zero_price_model():
    c = estimate_cost(TokenUsage(100, 100, 200, "estimated"), model("free", input_cost_per_million=0, output_cost_per_million=0))
    assert c.total_cost == 0.0
