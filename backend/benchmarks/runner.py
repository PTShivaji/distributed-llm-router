"""Load runners: fixed number of operations, fixed warm-up, configurable concurrency."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from benchmarks.stats import LatencyStats, summarize


@dataclass
class OpResult:
    ok: bool = True
    cache_status: str | None = None


@dataclass
class RunOutcome:
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    duration_s: float = 0.0
    cache_statuses: list[str | None] = field(default_factory=list)

    def stats(self, *, with_cache: bool = False) -> LatencyStats:
        return summarize(self.latencies_ms, errors=self.errors, duration_s=self.duration_s,
                         cache_statuses=self.cache_statuses if with_cache else None)

    def merge(self, other: "RunOutcome") -> "RunOutcome":
        return RunOutcome(self.latencies_ms + other.latencies_ms, self.errors + other.errors,
                          self.duration_s + other.duration_s, self.cache_statuses + other.cache_statuses)


AsyncOp = Callable[[int], Awaitable[OpResult]]
SyncOp = Callable[[int], object]


async def run_async(op: AsyncOp, *, requests: int, concurrency: int = 1, warmup: int = 0) -> RunOutcome:
    """Run `requests` measured calls of op(i) with `concurrency` workers after `warmup` calls.

    Each call's latency is measured individually (perf_counter); requests/second
    is completed operations divided by wall-clock time of the measured phase.
    An exception or OpResult(ok=False) counts as an error (latency still recorded).
    """
    if requests < 0 or concurrency < 1 or warmup < 0:
        raise ValueError("requests >= 0, concurrency >= 1, warmup >= 0 required")
    for i in range(warmup):
        await op(-1 - i)  # negative index: warm-up, never measured

    outcome = RunOutcome()
    next_index = iter(range(requests))

    async def worker() -> None:
        for i in next_index:  # shared iterator: each index taken exactly once
            t0 = time.perf_counter()
            try:
                result = await op(i)
            except Exception:
                result = OpResult(ok=False)
            outcome.latencies_ms.append((time.perf_counter() - t0) * 1000)
            outcome.cache_statuses.append(result.cache_status)
            if not result.ok:
                outcome.errors += 1

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(min(concurrency, max(requests, 1)))))
    outcome.duration_s = time.perf_counter() - started
    return outcome


def run_sync(op: SyncOp, *, requests: int, warmup: int = 0) -> RunOutcome:
    """Sequential CPU-bound micro-benchmark of op(i)."""
    for i in range(warmup):
        op(-1 - i)
    outcome = RunOutcome()
    started = time.perf_counter()
    for i in range(requests):
        t0 = time.perf_counter()
        try:
            op(i)
        except Exception:
            outcome.errors += 1
        outcome.latencies_ms.append((time.perf_counter() - t0) * 1000)
    outcome.duration_s = time.perf_counter() - started
    return outcome
