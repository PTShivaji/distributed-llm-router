"""Daily spend limits for REAL provider calls, shared by every instance.

How it works (per provider attempt, before the call is made):
  1. The WORST-CASE cost is computed: an upper bound for prompt tokens
     (UTF-8 bytes + framing) plus the full max_output_tokens, at the
     prompt-length pricing tier that size falls into.
  2. That amount is RESERVED atomically against the global daily budget, the
     per-client daily budget and the per-client daily token limit (one Lua
     script: either every counter is incremented or none is).
  3. After the call the reservation is SETTLED to the real cost:
       success            -> provider-reported usage (or the estimate if unreported)
       rejected by provider before work (auth, 400/404, 429, context window)
                          -> 0 (nothing was billed)
       timeout / 5xx / connection / unknown -> the reservation is KEPT (the provider may
                          have processed and billed the request)

Because reservations are made before calls, N concurrent requests can never
overshoot a limit by more than the difference between estimate and reality
(and the estimate is an upper bound).

Amounts are integer micro-USD. Keys share one hash tag per day, so the script
is Redis Cluster safe:  {prefix}budget:{YYYYMMDD}:global:usd
                        {prefix}budget:{YYYYMMDD}:client:<hmac>:usd|tokens
Counters expire two days after their last write.

Mock-provider calls cost nothing and never touch the budget.

Step 8A - one budget for every paid operation:
  * Chat attempts (`reserve`) and LiteLLM embedding calls (`reserve_cost`, see
    app/budget/embedding.py) reserve against the SAME global and per-client
    daily counters.
  * BUDGET_MAX_COST_PER_REQUEST_USD applies to the COMBINED spend of one chat
    request (embeddings + every chat attempt, retries and fallbacks included):
    ChatService opens a `request_budget()` scope; each reservation adds its
    worst case to the scope and each settlement corrects it to the real amount.
"""

import logging
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from app.core.logging import log_event
from app.infra.redis import REDIS_ERRORS, RedisManager
from app.providers.base import ProviderErrorKind
from app.ratelimit.limiter import hash_subject
from app.routing.registry import ModelConfig
from app.routing.usage import worst_case_cost

logger = logging.getLogger("app.budget")

MICROS_PER_USD = 1_000_000
COUNTER_TTL_MS = 2 * 86_400 * 1000

# Provider errors where the request was rejected before any (billable) work.
NOT_BILLED = frozenset({
    ProviderErrorKind.AUTH, ProviderErrorKind.BAD_REQUEST, ProviderErrorKind.NOT_FOUND,
    ProviderErrorKind.RATE_LIMIT, ProviderErrorKind.CONTEXT_WINDOW,
})

REASONS = ("per_request_limit", "global_daily_budget", "client_daily_budget", "client_daily_tokens",
           "budget_unavailable")


def usd_to_micros(usd: float) -> int:
    """Rounded UP, so a tiny non-zero cost is never counted as free."""
    return max(0, math.ceil(round(usd * MICROS_PER_USD, 6)))


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    daily_usd: float = 0.0
    daily_usd_per_client: float = 0.0
    daily_tokens_per_client: int = 0
    max_cost_per_request_usd: float = 0.0

    @property
    def tracks_daily(self) -> bool:
        return bool(self.daily_usd or self.daily_usd_per_client or self.daily_tokens_per_client)

    @property
    def configured(self) -> bool:
        return self.tracks_daily or bool(self.max_cost_per_request_usd)


@dataclass(slots=True)
class RequestSpend:
    """Running total (micro-USD) of one chat request: reserved-and-unsettled + settled amounts."""

    client_id: str | None = None
    micros: int = 0


_REQUEST_SPEND: ContextVar[RequestSpend | None] = ContextVar("llmr_request_spend", default=None)


@contextmanager
def request_budget(client_id: str | None) -> Iterator[RequestSpend]:
    """Scope for one chat request. Concurrent requests (separate asyncio tasks) never share it."""
    spend = RequestSpend(client_id=client_id)
    token = _REQUEST_SPEND.set(spend)
    try:
        yield spend
    finally:
        _REQUEST_SPEND.reset(token)


def current_request_spend() -> RequestSpend | None:
    return _REQUEST_SPEND.get()


@dataclass(frozen=True, slots=True)
class Reservation:
    day: str
    client: str  # HMAC of the client id, never the raw value
    cost_micros: int
    tokens: int
    stored: bool = True  # False: only the per-request limit applies (no daily counters configured)
    operation: str = "chat"  # chat | embedding (bounded; logs/metrics only)
    spend: RequestSpend | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    reservation: Reservation | None = None
    reason: str | None = None  # one of REASONS when rejected
    estimated_cost_usd: float = 0.0
    degraded: bool = False  # store unreachable


ALLOW_UNTRACKED = BudgetDecision(True)


class BudgetStore(Protocol):
    name: str

    async def reserve(self, day: str, client: str, cost: int, tokens: int,
                      limits: tuple[int, int, int]) -> int: ...

    async def adjust(self, day: str, client: str, cost_delta: int, token_delta: int) -> None: ...

    async def totals(self, day: str, client: str | None = None) -> dict[str, int]: ...


# --------------------------------------------------------------------------- #
# KEYS: global_usd, client_usd, client_tokens
# ARGV: cost, tokens, limit_global, limit_client_usd, limit_client_tokens, ttl_ms  (limit 0 = none)
# Returns 0 if reserved, else the 1-based index of the first exceeded limit (nothing written).
_RESERVE_LUA = """
local adds = {tonumber(ARGV[1]), tonumber(ARGV[1]), tonumber(ARGV[2])}
local limits = {tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5])}
for i = 1, 3 do
  if limits[i] > 0 then
    local current = tonumber(redis.call('GET', KEYS[i]) or '0')
    if current + adds[i] > limits[i] then return i end
  end
end
for i = 1, 3 do
  redis.call('INCRBY', KEYS[i], adds[i])
  redis.call('PEXPIRE', KEYS[i], ARGV[6])
end
return 0
"""

# KEYS as above; ARGV: cost_delta, token_delta, ttl_ms
_ADJUST_LUA = """
local deltas = {tonumber(ARGV[1]), tonumber(ARGV[1]), tonumber(ARGV[2])}
for i = 1, 3 do
  if deltas[i] ~= 0 then
    redis.call('INCRBY', KEYS[i], deltas[i])
    redis.call('PEXPIRE', KEYS[i], ARGV[3])
  end
end
return 0
"""


class RedisBudgetStore:
    name = "redis"

    def __init__(self, redis: RedisManager) -> None:
        self.redis = redis

    def _keys(self, day: str, client: str) -> list[str]:
        tag = "{" + day + "}"
        return [self.redis.keys("budget", tag, "global", "usd"),
                self.redis.keys("budget", tag, "client", client, "usd"),
                self.redis.keys("budget", tag, "client", client, "tokens")]

    async def reserve(self, day: str, client: str, cost: int, tokens: int, limits: tuple[int, int, int]) -> int:
        return int(await self.redis.client.eval(_RESERVE_LUA, 3, *self._keys(day, client), cost, tokens, *limits,
                                                COUNTER_TTL_MS))

    async def adjust(self, day: str, client: str, cost_delta: int, token_delta: int) -> None:
        await self.redis.client.eval(_ADJUST_LUA, 3, *self._keys(day, client), cost_delta, token_delta, COUNTER_TTL_MS)

    async def totals(self, day: str, client: str | None = None) -> dict[str, int]:
        keys = self._keys(day, client or "-")
        values = await self.redis.client.mget(keys if client else keys[:1])
        out = {"global_usd_micros": int(values[0] or 0)}
        if client:
            out |= {"client_usd_micros": int(values[1] or 0), "client_tokens": int(values[2] or 0)}
        return out


class LocalBudgetStore:
    """Process-local counters (REDIS_ENABLED=false). NOT shared between instances."""

    name = "memory"

    def __init__(self) -> None:
        self._counters: dict[tuple[str, ...], int] = {}

    def _keys(self, day: str, client: str) -> list[tuple[str, ...]]:
        return [(day, "global", "usd"), (day, client, "usd"), (day, client, "tokens")]

    async def reserve(self, day: str, client: str, cost: int, tokens: int, limits: tuple[int, int, int]) -> int:
        keys, adds = self._keys(day, client), (cost, cost, tokens)
        for i, (key, add, limit) in enumerate(zip(keys, adds, limits, strict=True), start=1):
            if limit > 0 and self._counters.get(key, 0) + add > limit:
                return i
        for key, add in zip(keys, adds, strict=True):
            self._counters[key] = self._counters.get(key, 0) + add
        return 0

    async def adjust(self, day: str, client: str, cost_delta: int, token_delta: int) -> None:
        for key, delta in zip(self._keys(day, client), (cost_delta, cost_delta, token_delta), strict=True):
            self._counters[key] = self._counters.get(key, 0) + delta

    async def totals(self, day: str, client: str | None = None) -> dict[str, int]:
        keys = self._keys(day, client or "-")
        out = {"global_usd_micros": self._counters.get(keys[0], 0)}
        if client:
            out |= {"client_usd_micros": self._counters.get(keys[1], 0), "client_tokens": self._counters.get(keys[2], 0)}
        return out


# --------------------------------------------------------------------------- #
class BudgetGuard:
    def __init__(self, store: BudgetStore, limits: BudgetLimits, *, fail_closed: bool = True,
                 clock=time.time) -> None:
        self.store = store
        self.limits = limits
        self.fail_closed = fail_closed
        self._clock = clock
        self._limit_micros = (usd_to_micros(limits.daily_usd), usd_to_micros(limits.daily_usd_per_client),
                              limits.daily_tokens_per_client)

    def day(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).strftime("%Y%m%d")

    def period(self) -> tuple[str, datetime]:
        """Read-only (GET /budget/status): the current day key AND the instant it was derived from,
        from ONE clock read, so the reported period always matches the counters that are read."""
        now = datetime.fromtimestamp(self._clock(), UTC)
        return now.strftime("%Y%m%d"), now

    @staticmethod
    def client_key(client_id: str | None) -> str:
        return hash_subject(client_id) if client_id else "anonymous"

    async def reserve(self, model: ModelConfig, client_id: str | None, input_tokens: int,
                      output_tokens: int) -> BudgetDecision:
        """Chat attempt: worst case at the prompt-length tier of `input_tokens`."""
        estimate = worst_case_cost(model, input_tokens, output_tokens)
        return await self._reserve(estimate, input_tokens + output_tokens, client_id, operation="chat",
                                   label=model.id)

    async def reserve_cost(self, cost_usd: float, tokens: int, client_id: str | None, *,
                           operation: str = "embedding", label: str = "embedding") -> BudgetDecision:
        """Generic reservation (step 8A): any paid operation with a precomputed worst-case cost."""
        if not math.isfinite(cost_usd) or cost_usd < 0 or tokens < 0:
            raise ValueError("cost_usd and tokens must be finite and non-negative")
        return await self._reserve(cost_usd, tokens, client_id, operation=operation, label=label)

    async def _reserve(self, estimate: float, tokens: int, client_id: str | None, *, operation: str,
                       label: str) -> BudgetDecision:
        spend = current_request_spend()
        cost = usd_to_micros(estimate)
        limit = usd_to_micros(self.limits.max_cost_per_request_usd)
        already = spend.micros if spend is not None else 0
        if limit and already + cost > limit:  # combined: chat + embeddings of this request
            return self._reject("per_request_limit", label, estimate, operation)
        day, client = self.day(), self.client_key(client_id)
        if not self.limits.tracks_daily:
            reservation = Reservation(day, client, cost, tokens, stored=False, operation=operation, spend=spend)
        else:
            try:
                failing = await self.store.reserve(day, client, cost, tokens, self._limit_micros)
            except REDIS_ERRORS as exc:
                log_event(logger, "budget.store_unavailable", logging.ERROR, error=type(exc).__name__,
                          fail_closed=self.fail_closed, model=label, operation=operation)
                if self.fail_closed:
                    return BudgetDecision(False, None, "budget_unavailable", estimate, degraded=True)
                return BudgetDecision(True, None, None, estimate, degraded=True)
            if failing:
                return self._reject(REASONS[failing], label, estimate, operation)
            reservation = Reservation(day, client, cost, tokens, stored=True, operation=operation, spend=spend)
            log_event(logger, "budget.reserved", model=label, operation=operation,
                      reserved_usd=cost / MICROS_PER_USD, tokens=tokens)
        if spend is not None:
            spend.micros += cost
        return BudgetDecision(True, reservation, None, estimate)

    async def settle(self, reservation: Reservation | None, *, cost_usd: float | None, tokens: int | None) -> None:
        """Replace the reservation with the actual amount (None = keep the reservation)."""
        if reservation is None:
            return
        cost_delta = 0 if cost_usd is None else usd_to_micros(cost_usd) - reservation.cost_micros
        token_delta = 0 if tokens is None else tokens - reservation.tokens
        if not cost_delta and not token_delta:
            return
        if reservation.spend is not None:
            reservation.spend.micros += cost_delta
        if not reservation.stored:
            return
        try:
            await self.store.adjust(reservation.day, reservation.client, cost_delta, token_delta)
        except REDIS_ERRORS as exc:  # the (larger) reservation stays counted: errs on the safe side
            log_event(logger, "budget.settle_failed", logging.WARNING, error=type(exc).__name__,
                      operation=reservation.operation)

    def _reject(self, reason: str, label: str, estimate: float, operation: str = "chat") -> BudgetDecision:
        log_event(logger, "budget.rejected", logging.WARNING, reason=reason, model=label, operation=operation,
                  estimated_usd=round(estimate, 6))
        return BudgetDecision(False, None, reason, estimate)
