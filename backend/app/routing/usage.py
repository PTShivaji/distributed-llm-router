"""Token usage and cost estimation.

Token counts are never invented: provider-reported values are used as-is and
labelled "provider_reported". Missing values are either estimated with a
clearly labelled heuristic (~4 characters per token) or left as unavailable.
"""

import math
from dataclasses import dataclass
from typing import Literal

from app.routing.registry import ModelConfig

# "cache_hit": no provider was called for this request -> 0 tokens, 0 cost.
UsageSource = Literal["provider_reported", "estimated", "partially_estimated", "unavailable", "cache_hit"]
CHARS_PER_TOKEN = 4
COST_DECIMALS = 8


def estimate_tokens(text: str) -> int:
    """Rough heuristic, NOT a tokenizer. Used only when the provider reports nothing."""
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    source: UsageSource


@dataclass(frozen=True, slots=True)
class CostEstimate:
    input_cost: float | None
    output_cost: float | None
    total_cost: float | None
    currency: str
    input_cost_per_million: float
    output_cost_per_million: float
    pricing_is_placeholder: bool


def build_usage(
    *,
    reported_input: int | None,
    reported_output: int | None,
    prompt_text: str,
    completion_text: str,
    estimate_missing: bool,
) -> TokenUsage:
    if reported_input is not None and reported_output is not None:
        return TokenUsage(reported_input, reported_output, reported_input + reported_output, "provider_reported")
    if not estimate_missing:
        return TokenUsage(reported_input, reported_output, None, "unavailable")

    input_tokens = reported_input if reported_input is not None else estimate_tokens(prompt_text)
    output_tokens = reported_output if reported_output is not None else estimate_tokens(completion_text)
    partial = reported_input is not None or reported_output is not None
    return TokenUsage(
        input_tokens, output_tokens, input_tokens + output_tokens, "partially_estimated" if partial else "estimated"
    )


# Upper bound for prompt tokens used by spend reservations (NOT for reporting):
# BPE / SentencePiece tokenizers emit at least one UTF-8 byte per token, plus a
# fixed allowance for role/message framing tokens.
MESSAGE_OVERHEAD_TOKENS = 32


def upper_bound_input_tokens(text: str) -> int:
    return len(text.encode("utf-8")) + MESSAGE_OVERHEAD_TOKENS


def worst_case_cost(model: ModelConfig, input_tokens: int, output_tokens: int) -> float:
    """USD for a request of this size, at the prompt-length tier that size falls into."""
    in_rate, out_rate = model.rates_for(input_tokens)
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def estimate_cost(usage: TokenUsage, model: ModelConfig, currency: str = "USD") -> CostEstimate:
    def cost(tokens: int | None, per_million: float) -> float | None:
        return None if tokens is None else round(tokens * per_million / 1_000_000, COST_DECIMALS)

    in_rate, out_rate = model.rates_for(usage.input_tokens)
    input_cost = cost(usage.input_tokens, in_rate)
    output_cost = cost(usage.output_tokens, out_rate)
    total = None if input_cost is None or output_cost is None else round(input_cost + output_cost, COST_DECIMALS)
    return CostEstimate(
        input_cost=input_cost,
        output_cost=output_cost,
        total_cost=total,
        currency=currency,
        input_cost_per_million=in_rate,
        output_cost_per_million=out_rate,
        pricing_is_placeholder=model.pricing_is_placeholder,
    )
