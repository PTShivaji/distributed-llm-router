"""Provider health tracking / circuit breaker."""

from app.routing.health import HealthState, HealthTracker
from tests.conftest import FakeClock


def tracker(clock=None, threshold=3, cooldown=30):
    return HealthTracker(failure_threshold=threshold, cooldown_seconds=cooldown, clock=clock or FakeClock())


def test_unknown_model_is_healthy():
    t = tracker()
    assert t.state("x") == HealthState.HEALTHY and t.is_available("x")


def test_failures_degrade_then_open_circuit():
    t = tracker(threshold=3)
    t.record_failure("m", "timeout")
    assert t.state("m") == HealthState.DEGRADED and t.is_available("m")
    t.record_failure("m", "timeout")
    t.record_failure("m", "unavailable")
    assert t.state("m") == HealthState.UNHEALTHY and not t.is_available("m")
    snap = t.snapshot("m")
    assert snap.consecutive_failures == 3 and snap.last_error_kind == "unavailable"


def test_success_resets_consecutive_failures():
    t = tracker(threshold=2)
    t.record_failure("m", "timeout")
    t.record_success("m")
    t.record_failure("m", "timeout")
    assert t.state("m") == HealthState.DEGRADED
    snap = t.snapshot("m")
    assert snap.total_failures == 2 and snap.total_successes == 1


def test_cooldown_allows_half_open_probe():
    clock = FakeClock()
    t = tracker(clock, threshold=1, cooldown=30)
    t.record_failure("m", "unavailable")
    assert not t.is_available("m")
    clock.now += 31
    assert t.is_available("m") and t.state("m") == HealthState.DEGRADED
    t.record_success("m")
    assert t.state("m") == HealthState.HEALTHY


def test_models_tracked_independently():
    t = tracker(threshold=1)
    t.record_failure("a", "auth")
    assert not t.is_available("a") and t.is_available("b")
