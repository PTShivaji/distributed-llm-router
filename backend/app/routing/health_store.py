"""Health storage backends: process-local (Phase 3) and Redis (shared, Phase 4).

Routing reads health through a synchronous *view* (a snapshot taken once per
request); updates go through async `record_*` calls.

Redis backend
-------------
Key: {prefix}health:{model_id}  HASH consecutive_failures, total_successes,
     total_failures, last_error_kind, last_failure_at   (TTL: HEALTH_KEY_TTL_SECONDS)
Key: {prefix}health:probe:{model_id}  recovery-probe lock (SET NX PX)

- Updates are single Lua scripts (atomic across instances) using Redis TIME,
  so every instance shares one clock for cooldowns.
- State is computed from the stored counters by one pure function
  (`compute_state`) used by both reads and state-change detection.
- Recovery probe: after the cooldown, a model is "half-open". Only the
  instance that wins the probe lock may call it; a success resets it, a
  failure re-opens the circuit for another cooldown.

Redis outage: the store falls back to an in-process HealthTracker and logs
`health.redis_unavailable`. This is safe because health is advisory (it only
orders/skips candidates); the worst case is retrying a model another instance
already knows is down. Local fallback state is not merged back into Redis.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from app.core.logging import log_event
from app.infra.redis import REDIS_ERRORS, RedisManager
from app.routing.health import HealthSnapshot, HealthState, HealthTracker

logger = logging.getLogger("app.routing.health")

_FAILURE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local prev_cf = tonumber(redis.call('HGET', KEYS[1], 'consecutive_failures') or '0')
local prev_last = redis.call('HGET', KEYS[1], 'last_failure_at') or '0'
local cf = redis.call('HINCRBY', KEYS[1], 'consecutive_failures', 1)
redis.call('HINCRBY', KEYS[1], 'total_failures', 1)
redis.call('HSET', KEYS[1], 'last_error_kind', ARGV[1], 'last_failure_at', string.format('%.6f', now))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
redis.call('DEL', KEYS[2])
return {prev_cf, prev_last, cf, string.format('%.6f', now)}
"""

_SUCCESS_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local prev_cf = tonumber(redis.call('HGET', KEYS[1], 'consecutive_failures') or '0')
local prev_last = redis.call('HGET', KEYS[1], 'last_failure_at') or '0'
redis.call('HSET', KEYS[1], 'consecutive_failures', 0)
redis.call('HINCRBY', KEYS[1], 'total_successes', 1)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
redis.call('DEL', KEYS[2])
return {prev_cf, prev_last, string.format('%.6f', now)}
"""


def compute_state(
    consecutive_failures: int, last_failure_at: float, now: float, threshold: int, cooldown: float
) -> tuple[HealthState, bool]:
    """Returns (state, needs_probe). Same rules as the local HealthTracker."""
    if consecutive_failures <= 0:
        return HealthState.HEALTHY, False
    if consecutive_failures >= threshold:
        if now - last_failure_at < cooldown:
            return HealthState.UNHEALTHY, False
        return HealthState.DEGRADED, True  # half-open: one probe allowed
    return HealthState.DEGRADED, False


class HealthView(Protocol):
    def state(self, model_id: str) -> HealthState: ...
    def is_available(self, model_id: str) -> bool: ...
    def snapshot(self, model_id: str) -> HealthSnapshot: ...
    def needs_probe(self, model_id: str) -> bool: ...


class HealthStore(Protocol):
    backend: str

    async def view(self, model_ids: list[str]) -> HealthView: ...
    async def record_success(self, model_id: str) -> HealthState:
        """Atomically record a success and return the model's state right after it."""
        ...
    async def record_failure(self, model_id: str, kind: str) -> None: ...
    async def acquire_probe(self, model_id: str) -> bool: ...


# --------------------------------------------------------------------------- #
class _LocalView:
    def __init__(self, tracker: HealthTracker) -> None:
        self._t = tracker

    def state(self, model_id: str) -> HealthState:
        return self._t.state(model_id)

    def is_available(self, model_id: str) -> bool:
        return self._t.is_available(model_id)

    def snapshot(self, model_id: str) -> HealthSnapshot:
        return self._t.snapshot(model_id)

    def needs_probe(self, model_id: str) -> bool:
        return False


class LocalHealthStore:
    """Process-local health (Phase 3 behaviour). Not shared between instances."""

    backend = "memory"

    def __init__(self, tracker: HealthTracker | None = None) -> None:
        self.tracker = tracker or HealthTracker()

    async def view(self, model_ids: list[str]) -> HealthView:
        return _LocalView(self.tracker)

    async def record_success(self, model_id: str) -> HealthState:
        before = self.tracker.state(model_id)
        self.tracker.record_success(model_id)
        after = self.tracker.state(model_id)
        _log_change(model_id, before, after, self.backend)
        return after

    async def record_failure(self, model_id: str, kind: str) -> None:
        before = self.tracker.state(model_id)
        self.tracker.record_failure(model_id, kind)
        _log_change(model_id, before, self.tracker.state(model_id), self.backend)

    async def acquire_probe(self, model_id: str) -> bool:
        return True


# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class _Entry:
    snapshot: HealthSnapshot
    needs_probe: bool


class _StaticView:
    def __init__(self, entries: dict[str, _Entry]) -> None:
        self._entries = entries

    def _get(self, model_id: str) -> _Entry:
        return self._entries.get(model_id) or _Entry(HealthSnapshot(HealthState.HEALTHY, 0, 0, 0, None), False)

    def state(self, model_id: str) -> HealthState:
        return self._get(model_id).snapshot.state

    def is_available(self, model_id: str) -> bool:
        return self.state(model_id) != HealthState.UNHEALTHY

    def snapshot(self, model_id: str) -> HealthSnapshot:
        return self._get(model_id).snapshot

    def needs_probe(self, model_id: str) -> bool:
        return self._get(model_id).needs_probe


class RedisHealthStore:
    backend = "redis"

    def __init__(
        self,
        redis: RedisManager,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        key_ttl_seconds: int = 86_400,
        probe_ttl_seconds: float = 30.0,
    ) -> None:
        self.redis = redis
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.key_ttl_seconds = key_ttl_seconds
        self.probe_ttl_seconds = probe_ttl_seconds
        self.fallback = LocalHealthStore(HealthTracker(failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds))
        self.degraded = False

    def _key(self, model_id: str) -> str:
        return self.redis.keys("health", model_id)

    def _probe_key(self, model_id: str) -> str:
        return self.redis.keys("health", "probe", model_id)

    def _on_outage(self, op: str, exc: BaseException) -> None:
        if not self.degraded:
            log_event(logger, "health.redis_unavailable", logging.WARNING, op=op, error=type(exc).__name__,
                      fallback="process_local")
        self.degraded = True

    def _on_recovered(self) -> None:
        if self.degraded:
            log_event(logger, "health.redis_recovered")
        self.degraded = False

    # ------------------------------------------------------------------ #
    async def view(self, model_ids: list[str]) -> HealthView:
        try:
            client = self.redis.client
            async with client.pipeline(transaction=False) as pipe:
                pipe.time()
                for mid in model_ids:
                    pipe.hgetall(self._key(mid))
                results = await pipe.execute()
        except REDIS_ERRORS as exc:
            self._on_outage("view", exc)
            return await self.fallback.view(model_ids)
        self._on_recovered()

        seconds, micros = results[0]
        now = seconds + micros / 1_000_000
        entries = {}
        for mid, raw in zip(model_ids, results[1:]):
            data = {k.decode(): v.decode() for k, v in raw.items()}
            cf = int(data.get("consecutive_failures", 0))
            state, probe = compute_state(cf, float(data.get("last_failure_at", 0)), now,
                                         self.failure_threshold, self.cooldown_seconds)
            entries[mid] = _Entry(
                HealthSnapshot(state, cf, int(data.get("total_successes", 0)), int(data.get("total_failures", 0)),
                               data.get("last_error_kind")),
                probe,
            )
        return _StaticView(entries)

    async def record_success(self, model_id: str) -> HealthState:
        try:
            prev_cf, prev_last, now = await self.redis.client.eval(
                _SUCCESS_LUA, 2, self._key(model_id), self._probe_key(model_id), self.key_ttl_seconds
            )
        except REDIS_ERRORS as exc:
            self._on_outage("record_success", exc)
            return await self.fallback.record_success(model_id)
        self._on_recovered()
        before, _ = compute_state(int(prev_cf), float(prev_last), float(now), self.failure_threshold, self.cooldown_seconds)
        # The script set consecutive_failures=0 atomically, so the state right after it is HEALTHY.
        _log_change(model_id, before, HealthState.HEALTHY, self.backend)
        return HealthState.HEALTHY

    async def record_failure(self, model_id: str, kind: str) -> None:
        try:
            prev_cf, prev_last, cf, now = await self.redis.client.eval(
                _FAILURE_LUA, 2, self._key(model_id), self._probe_key(model_id), str(kind), self.key_ttl_seconds
            )
        except REDIS_ERRORS as exc:
            self._on_outage("record_failure", exc)
            await self.fallback.record_failure(model_id, kind)
            return
        self._on_recovered()
        t, c = self.failure_threshold, self.cooldown_seconds
        before, _ = compute_state(int(prev_cf), float(prev_last), float(now), t, c)
        after, _ = compute_state(int(cf), float(now), float(now), t, c)
        _log_change(model_id, before, after, self.backend)

    async def acquire_probe(self, model_id: str) -> bool:
        try:
            got = await self.redis.client.set(
                self._probe_key(model_id), "1", nx=True, px=int(self.probe_ttl_seconds * 1000)
            )
        except REDIS_ERRORS as exc:
            self._on_outage("acquire_probe", exc)
            return True  # cannot coordinate: allow the probe locally
        if got:
            log_event(logger, "health.probe_acquired", model=model_id)
        return bool(got)


def _log_change(model_id: str, before: HealthState, after: HealthState, backend: str) -> None:
    if before != after:
        level = logging.WARNING if after == HealthState.UNHEALTHY else logging.INFO
        log_event(logger, "health.state_change", level, model=model_id, from_state=before, to_state=after, backend=backend)
