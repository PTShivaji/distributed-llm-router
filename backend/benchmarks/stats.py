"""Latency statistics.

Percentiles use linear interpolation between closest ranks - the same
definition as numpy.percentile(method="linear") and Excel PERCENTILE.INC:
    rank = p/100 * (n - 1);  value = v[floor] + (v[ceil] - v[floor]) * frac(rank)
"""

import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any

CACHE_HIT_STATUSES = frozenset({"hit_exact", "hit_semantic"})
CACHE_LOOKUP_STATUSES = CACHE_HIT_STATUSES | {"miss"}


def percentile(values: list[float], p: float) -> float | None:
    """p in [0, 100]. `values` need not be sorted. Empty -> None."""
    if not 0 <= p <= 100:
        raise ValueError("percentile must be between 0 and 100")
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = p / 100 * (len(ordered) - 1)
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


@dataclass
class LatencyStats:
    count: int
    errors: int
    error_rate: float
    mean_ms: float | None
    median_ms: float | None
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    min_ms: float | None
    max_ms: float | None
    stdev_ms: float | None
    duration_s: float
    requests_per_second: float | None
    cache_hit_ratio: float | None = None
    cache_lookups: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def summarize(
    latencies_ms: list[float],
    *,
    errors: int = 0,
    duration_s: float,
    cache_statuses: list[str | None] | None = None,
) -> LatencyStats:
    """`latencies_ms` holds one entry per completed operation (successful or not)."""
    n = len(latencies_ms)
    hits = lookups = None
    if cache_statuses is not None:
        hits = sum(1 for s in cache_statuses if s in CACHE_HIT_STATUSES)
        lookups = sum(1 for s in cache_statuses if s in CACHE_LOOKUP_STATUSES)
    return LatencyStats(
        count=n,
        errors=errors,
        error_rate=(errors / n) if n else 0.0,
        mean_ms=statistics.fmean(latencies_ms) if n else None,
        median_ms=statistics.median(latencies_ms) if n else None,
        p50_ms=percentile(latencies_ms, 50),
        p95_ms=percentile(latencies_ms, 95),
        p99_ms=percentile(latencies_ms, 99),
        min_ms=min(latencies_ms) if n else None,
        max_ms=max(latencies_ms) if n else None,
        stdev_ms=statistics.stdev(latencies_ms) if n > 1 else None,
        duration_s=duration_s,
        requests_per_second=(n / duration_s) if duration_s > 0 and n else None,
        cache_hit_ratio=(hits / lookups) if lookups else None,
        cache_lookups=lookups,
    )
