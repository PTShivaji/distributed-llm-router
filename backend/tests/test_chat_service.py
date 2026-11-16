"""ChatService orchestration: retries, timeouts, fallback, exhaustion, tracking."""

import asyncio
import logging

import pytest

from app.providers.base import LLMProvider, ProviderError, ProviderErrorKind
from app.providers.mock import MockProvider
from app.routing.errors import AllModelsFailedError, UnknownModelError
from app.routing.health import HealthState, HealthTracker
from app.routing.service import ChatCommand
from tests.conftest import model

SIMPLE_Q = "What is the capital of France?"
COMPLEX_Q = (
    "Design a distributed rate limiter for a multi-region API gateway that must handle 1M requests "
    "per second. Compare token bucket and sliding window approaches and analyze the trade-offs."
)


def chat(service, query=SIMPLE_Q, **kw):
    return asyncio.run(service.chat(ChatCommand(query=query, request_id="test-rid", **kw)))


# --------------------------------------------------------------------------- #
# Success path & tracking
# --------------------------------------------------------------------------- #
def test_success_records_everything(make_service, three_tier_models):
    out = chat(make_service(three_tier_models))
    assert out.request_id == "test-rid"
    assert out.answer.startswith("[MOCK:fast]")
    assert out.selected_model == out.model_used == "fast"
    assert out.classification.complexity == "SIMPLE"
    assert not out.fallback.used and out.fallback.reason is None
    assert [a.outcome for a in out.fallback.attempts] == ["success"]
    assert out.usage.source == "estimated"
    assert out.cost.total_cost is not None and out.cost.pricing_is_placeholder
    assert out.latency_ms >= 0
    assert out.provider_status == HealthState.HEALTHY
    assert out.mock_mode


def test_provider_reported_usage_flows_to_cost(make_service, three_tier_models):
    out = chat(make_service(three_tier_models, report_usage=True))
    assert out.usage.source == "provider_reported"
    assert out.usage.total_tokens == out.usage.input_tokens + out.usage.output_tokens
    expected = round(out.usage.input_tokens * 1.0 / 1e6 + out.usage.output_tokens * 2.0 / 1e6, 8)
    assert out.cost.total_cost == pytest.approx(expected)


def test_complex_query_uses_advanced(make_service, three_tier_models):
    out = chat(make_service(three_tier_models), COMPLEX_Q)
    assert out.model_used == "advanced" and out.routing.routed_tier == "COMPLEX"


def test_max_output_tokens_clamped_to_model_limit(make_service):
    seen = {}

    class Spy(MockProvider):
        async def complete(self, request):
            seen["max"] = request.max_output_tokens
            seen["temperature"] = request.temperature
            return await super().complete(request)

    svc = make_service([model("m", max_output_tokens=256)], provider=Spy())
    chat(svc, max_output_tokens=5000, temperature=0.2)
    assert seen == {"max": 256, "temperature": 0.2}


# --------------------------------------------------------------------------- #
# Retries
# --------------------------------------------------------------------------- #
def test_transient_error_retried_then_succeeds(make_service):
    svc = make_service([model("m")], failures={"m": "flaky"}, max_retries=1, retry_backoff_seconds=0.5)
    out = chat(svc)
    assert out.model_used == "m" and not out.fallback.used
    assert [a.outcome for a in out.fallback.attempts] == ["error", "success"]
    assert svc._sleep.delays == [0.5]


def test_retry_backoff_is_exponential(make_service):
    svc = make_service([model("m")], failures={"m": "unavailable"}, max_retries=3, retry_backoff_seconds=0.1, fallback_enabled=False)
    with pytest.raises(AllModelsFailedError):
        chat(svc)
    assert svc._sleep.delays == [0.1, 0.2, 0.4]


@pytest.mark.parametrize("mode", ["auth", "bad_request", "content_policy"])
def test_permanent_errors_not_retried(make_service, mode):
    svc = make_service([model("m")], failures={"m": mode}, max_retries=3, fallback_enabled=False)
    with pytest.raises(AllModelsFailedError):
        chat(svc)
    assert svc.providers.mock.calls["m"] == 1
    assert svc._sleep.delays == []


# --------------------------------------------------------------------------- #
# Timeouts
# --------------------------------------------------------------------------- #
def test_timeout_falls_back_without_retrying_same_model(make_service):
    models = [model("slow", timeout_seconds=0.05, fallbacks=("backup",)), model("backup", priority=200)]
    svc = make_service(models, failures={"slow": "timeout"}, max_retries=3)
    out = chat(svc)
    assert out.selected_model == "slow" and out.model_used == "backup"
    assert [(a.model_id, a.error_kind) for a in out.fallback.attempts] == [("slow", "timeout"), ("backup", None)]
    assert svc.providers.mock.calls["slow"] == 1  # not retried
    assert "timeout" in out.fallback.reason


def test_default_timeout_used_when_model_has_none(make_service):
    svc = make_service([model("m", timeout_seconds=None)], failures={"m": "timeout"},
                       default_timeout_seconds=0.05, fallback_enabled=False)
    with pytest.raises(AllModelsFailedError) as info:
        chat(svc)
    assert info.value.details["attempts"][0]["error_kind"] == "timeout"


# --------------------------------------------------------------------------- #
# Fallback
# --------------------------------------------------------------------------- #
def test_fallback_success_records_original_and_final(make_service, three_tier_models):
    svc = make_service(three_tier_models, failures={"advanced": "unavailable"}, max_retries=1)
    out = chat(svc, COMPLEX_Q)
    assert out.fallback.used
    assert out.fallback.original_model == out.selected_model == "advanced"
    assert out.fallback.final_model == out.model_used == "balanced"
    assert [a.outcome for a in out.fallback.attempts] == ["error", "error", "success"]
    assert out.cost.input_cost_per_million == 1.0  # cost uses the model actually used


def test_fallback_exhaustion_raises_with_safe_details(make_service, three_tier_models):
    fail_all = {m.id: "unavailable" for m in three_tier_models}
    svc = make_service(three_tier_models, failures=fail_all, max_retries=0)
    with pytest.raises(AllModelsFailedError) as info:
        chat(svc)
    err = info.value
    assert err.status_code == 503 and err.code == "all_models_failed"
    assert [a["model"] for a in err.details["attempts"]] == ["fast", "balanced", "advanced"]
    assert all(a["error_kind"] == "unavailable" for a in err.details["attempts"])


def test_content_policy_does_not_fall_back(make_service, three_tier_models):
    svc = make_service(three_tier_models, failures={"fast": "content_policy"})
    with pytest.raises(AllModelsFailedError) as info:
        chat(svc)
    assert info.value.code == "request_rejected_by_provider" and info.value.status_code == 422
    assert svc.providers.mock.calls["balanced"] == 0


def test_fallback_disabled(make_service, three_tier_models):
    svc = make_service(three_tier_models, failures={"fast": "auth"}, fallback_enabled=False)
    with pytest.raises(AllModelsFailedError):
        chat(svc)
    assert svc.providers.mock.calls["balanced"] == 0


def test_mutual_fallbacks_terminate(make_service):
    models = [model("a", fallbacks=("b",)), model("b", fallbacks=("a",))]
    svc = make_service(models, failures={"a": "unavailable", "b": "unavailable"}, max_retries=0)
    with pytest.raises(AllModelsFailedError) as info:
        chat(svc)
    assert [a["model"] for a in info.value.details["attempts"]] == ["a", "b"]


def test_override_does_not_fall_back_by_default(make_service, three_tier_models):
    svc = make_service(three_tier_models, failures={"advanced": "unavailable"}, max_retries=0)
    with pytest.raises(AllModelsFailedError):
        chat(svc, model_override="advanced")
    assert svc.providers.mock.calls["balanced"] == 0


def test_override_falls_back_when_enabled(make_service, three_tier_models):
    svc = make_service(three_tier_models, failures={"advanced": "unavailable"}, max_retries=0, fallback_on_override=True)
    out = chat(svc, model_override="advanced")
    assert out.routing.override and out.model_used == "balanced"


def test_unknown_override_raises_before_any_provider_call(make_service, three_tier_models):
    svc = make_service(three_tier_models)
    with pytest.raises(UnknownModelError):
        chat(svc, model_override="nope")
    assert sum(svc.providers.mock.calls.values()) == 0


# --------------------------------------------------------------------------- #
# Health integration
# --------------------------------------------------------------------------- #
def test_failures_update_health_and_unhealthy_fallbacks_are_skipped(make_service):
    health = HealthTracker(failure_threshold=1, cooldown_seconds=60)
    models = [model("p", priority=1, fallbacks=("sick", "ok")), model("sick", priority=2), model("ok", priority=3)]
    health.record_failure("sick", "unavailable")  # circuit open for 'sick'
    svc = make_service(models, failures={"p": "auth"}, health=health, policy_kwargs={"fallback_auto_tier": False})
    out = chat(svc)
    assert [(a.model_id, a.outcome) for a in out.fallback.attempts] == [
        ("p", "error"), ("sick", "skipped_unhealthy"), ("ok", "success"),
    ]
    assert health.state("p") == HealthState.UNHEALTHY


def test_request_errors_do_not_mark_model_unhealthy(make_service):
    health = HealthTracker(failure_threshold=1)
    svc = make_service([model("m")], failures={"m": "bad_request"}, health=health, fallback_enabled=False)
    with pytest.raises(AllModelsFailedError):
        chat(svc)
    assert health.state("m") == HealthState.HEALTHY


def test_unexpected_provider_exception_is_logged_and_contained(make_service, caplog):
    class Broken(LLMProvider):
        name = "broken"

        async def complete(self, request):
            raise KeyError("internal adapter bug")

    svc = make_service([model("m")], provider=Broken(), fallback_enabled=False)
    with caplog.at_level(logging.ERROR), pytest.raises(AllModelsFailedError) as info:
        chat(svc)
    assert info.value.details["attempts"][0]["error_kind"] == "unknown"
    assert any("provider.unexpected_error" in r.getMessage() and r.exc_info for r in caplog.records)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def test_structured_logs_cover_lifecycle_without_user_text(make_service, three_tier_models, caplog):
    secret_query = "My password is hunter2. What is the capital of France?"
    svc = make_service(three_tier_models, failures={"fast": "unavailable"}, max_retries=0)
    with caplog.at_level(logging.INFO, logger="app.routing"):
        chat(svc, secret_query)
    messages = [r.getMessage() for r in caplog.records]
    for event in ["chat.request_received", "chat.classified", "chat.model_selected", "provider.invoke",
                  "provider.failure", "chat.fallback_attempt", "chat.completed"]:
        assert any(m.startswith(event) for m in messages), event
    assert not any("hunter2" in m for m in messages)
    completed = next(m for m in messages if m.startswith("chat.completed"))
    assert "latency_ms=" in completed and "input_tokens=" in completed and "model_used=balanced" in completed


def test_provider_error_kind_enum_values_are_strings():
    assert ProviderError(ProviderErrorKind.TIMEOUT, "x", provider="p").kind == "timeout"
