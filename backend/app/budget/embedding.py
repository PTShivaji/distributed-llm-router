"""Step 8A: spend protection for paid (LiteLLM) embedding calls.

`BudgetedEmbedder` wraps the real embedder. For EVERY embedding call:

  1. Worst case: upper_bound_input_tokens(text) (UTF-8 bytes + 32 framing tokens)
     x EMBEDDING_INPUT_COST_PER_MILLION. Embeddings have no output tokens.
  2. Reserve it with the SAME BudgetGuard (and therefore the same Redis counters)
     as chat: global daily, per-client daily USD + tokens, and the per-request
     limit, which is shared with the chat attempts of the same request.
  3. Refused, OR the budget store is unreachable (even with BUDGET_FAIL_CLOSED=false:
     an embedding is only an optimisation, so it is never made without a
     reservation) -> EmbeddingBudgetRefused is raised and the provider is NOT
     called. The cache then skips approximate search (exact-match caching continues).
  4. Settle:
       success with reported usage -> reported input tokens x price
       success without usage        -> reservation KEPT (unknown real amount)
       failure that certainly did no billable work (auth, 400, 404, 429,
       context window)              -> released to 0
       any other failure (timeout, 5xx, connection, malformed, unknown,
       cancellation)                -> reservation KEPT

The local hashing embedder is never wrapped: it is free and never touches the budget.
Prompts and vectors are never logged; only sizes, reasons and amounts.
"""

import logging
from typing import TYPE_CHECKING

from app.budget.guard import NOT_BILLED, BudgetGuard, current_request_spend
from app.core.logging import log_event
from app.embeddings.base import EmbeddingBudgetRefused, EmbeddingError, EmbeddingProvider
from app.routing.usage import upper_bound_input_tokens

if TYPE_CHECKING:
    from app.observability.metrics import Metrics

logger = logging.getLogger("app.budget.embedding")


def embedding_cost(tokens: int, input_cost_per_million: float) -> float:
    return tokens * input_cost_per_million / 1_000_000


class BudgetedEmbedder(EmbeddingProvider):
    def __init__(self, inner: EmbeddingProvider, guard: BudgetGuard, *, input_cost_per_million: float,
                 metrics: "Metrics | None" = None) -> None:
        if not input_cost_per_million > 0:
            raise ValueError("a positive embedding input price is required")
        self.inner = inner
        self.guard = guard
        self.input_cost_per_million = input_cost_per_million
        self.metrics = metrics
        self.id = inner.id  # same vector space id: cache scopes are unchanged
        self.is_semantic = inner.is_semantic

    @property  # type: ignore[override]
    def dimensions(self) -> int:
        return self.inner.dimensions

    def _event(self, event: str, reason: str | None = None) -> None:
        if self.metrics is not None:
            self.metrics.embedding_budget(event, reason)

    async def embed(self, text: str) -> list[float]:
        vector, _ = await self.embed_with_usage(text)
        return vector

    async def embed_with_usage(self, text: str) -> tuple[list[float], int | None]:
        tokens = upper_bound_input_tokens(text)
        worst = embedding_cost(tokens, self.input_cost_per_million)
        spend = current_request_spend()
        decision = await self.guard.reserve_cost(worst, tokens, spend.client_id if spend else None,
                                                 operation="embedding", label=self.id)
        if not decision.allowed or decision.degraded:
            reason = decision.reason or "budget_unavailable"
            self._event("refused", reason)
            log_event(logger, "embedding.budget_refused", logging.WARNING, reason=reason, provider=self.id,
                      estimated_usd=round(worst, 8), input_chars=len(text))
            raise EmbeddingBudgetRefused(reason)
        reservation = decision.reservation
        self._event("reserved")
        try:
            vector, reported = await self.inner.embed_with_usage(text)
        except EmbeddingError as exc:
            if exc.kind is not None and exc.kind in NOT_BILLED:
                await self.guard.settle(reservation, cost_usd=0.0, tokens=0)
                self._event("released")
            else:
                self._event("kept_uncertain")  # may have been billed: keep the full reservation
            raise
        except BaseException:  # cancellation / unexpected: the call may have reached the provider
            self._event("kept_uncertain")
            raise
        if reported is None:
            self._event("kept_no_usage")
            return vector, None
        actual = embedding_cost(reported, self.input_cost_per_million)
        await self.guard.settle(reservation, cost_usd=actual, tokens=reported)
        self._event("settled")
        if self.metrics is not None:
            self.metrics.embedding_cost(actual)
        return vector, reported
