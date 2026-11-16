"""In-memory per-model health tracking with a simple circuit breaker.

    HEALTHY   -> no recent consecutive failures
    DEGRADED  -> some consecutive failures, still routable
    UNHEALTHY -> >= threshold consecutive failures within the cooldown window:
                 skipped by routing/fallback. After the cooldown one request is
                 allowed through again (half-open); a success resets it.

State is process-local. (Sharing it across instances via Redis is Phase 4.)
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class ModelHealth:
    consecutive_failures: int = 0
    total_successes: int = 0
    total_failures: int = 0
    last_error_kind: str | None = None
    last_failure_at: float | None = None


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    state: HealthState
    consecutive_failures: int
    total_successes: int
    total_failures: int
    last_error_kind: str | None


class HealthTracker:
    def __init__(
        self, *, failure_threshold: int = 3, cooldown_seconds: float = 30.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._models: dict[str, ModelHealth] = {}

    def _get(self, model_id: str) -> ModelHealth:
        return self._models.setdefault(model_id, ModelHealth())

    def record_success(self, model_id: str) -> None:
        h = self._get(model_id)
        h.consecutive_failures = 0
        h.total_successes += 1

    def record_failure(self, model_id: str, kind: str) -> None:
        h = self._get(model_id)
        h.consecutive_failures += 1
        h.total_failures += 1
        h.last_error_kind = kind
        h.last_failure_at = self._clock()

    def state(self, model_id: str) -> HealthState:
        h = self._models.get(model_id)
        if h is None or h.consecutive_failures == 0:
            return HealthState.HEALTHY
        if h.consecutive_failures >= self.failure_threshold and not self._cooled_down(h):
            return HealthState.UNHEALTHY
        return HealthState.DEGRADED

    def is_available(self, model_id: str) -> bool:
        return self.state(model_id) != HealthState.UNHEALTHY

    def snapshot(self, model_id: str) -> HealthSnapshot:
        h = self._models.get(model_id, ModelHealth())
        return HealthSnapshot(
            state=self.state(model_id),
            consecutive_failures=h.consecutive_failures,
            total_successes=h.total_successes,
            total_failures=h.total_failures,
            last_error_kind=h.last_error_kind,
        )

    def _cooled_down(self, h: ModelHealth) -> bool:
        return h.last_failure_at is not None and self._clock() - h.last_failure_at >= self.cooldown_seconds
