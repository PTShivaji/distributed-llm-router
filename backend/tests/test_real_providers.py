"""Phase 5 step 8: real provider integration - with FAKE clients only.

No test here makes a network call: litellm.acompletion is replaced through
dependency injection (FakeCompletion). LiteLLM's real exception classes are
constructed locally to test error mapping. Keys are fake strings.
"""

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.core.config import PROJECT_ROOT
from app.main import create_app
from app.providers.base import ProviderErrorKind, ProviderRequest
from app.providers.catalog import SUPPORTED_PROVIDERS, ProviderConfigError, check_provider_configuration
from app.providers.litellm_provider import LiteLLMProvider
from app.routing.errors import AllModelsFailedError
from app.routing.factory import build_chat_service
from app.routing.registry import ModelRegistry, load_registry
from app.routing.service import ChatCommand
from tests.provider_fakes import (
    FAKE_KEYS, PROVIDER_MODELS, PROVIDERS, FakeCompletion, all_provider_registry, real_model, real_settings,
)

Q = "What is the capital of France?"
EXAMPLE = PROJECT_ROOT / "backend" / "config" / "models.providers.example.json"


@pytest.fixture(scope="module")
def lx():
    """LiteLLM's exception module (imported once; ~8 s; no network, no .env loading)."""
    from app.providers.litellm_env import import_litellm

    return import_litellm().exceptions


def chat(service, model: str | None = None, query: str = Q, client_id: str | None = "client-a"):
    return asyncio.run(service.chat(ChatCommand(query=query, request_id="req-1", model_override=model,
                                                client_id=client_id)))


def service_for(fake: FakeCompletion, registry: ModelRegistry | None = None, **settings):
    return build_chat_service(real_settings(**settings), registry=registry or all_provider_registry(),
                              completion_fn=fake)


# =========================================================================== #
# 1. Real calls require explicit configuration (startup gating)
# =========================================================================== #
def test_mock_mode_is_default_and_never_builds_a_real_client():
    from app.core.config import Settings

    s = Settings(_env_file=None)
    assert s.llm_mock_mode is True and s.allow_real_provider_calls is False and not s.real_provider_calls_enabled
    service = build_chat_service(s)
    assert service.providers._real_factory is None  # no LiteLLM adapter can even be created


def test_mock_mode_off_alone_refuses_to_start():
    with pytest.raises(ProviderConfigError) as exc:
        build_chat_service(real_settings(allow_real_provider_calls=False), registry=all_provider_registry())
    assert "ALLOW_REAL_PROVIDER_CALLS" in str(exc.value)


def test_allow_flag_alone_keeps_mock_mode():
    service = build_chat_service(real_settings(llm_mock_mode=True), registry=all_provider_registry())
    m = service.policy.registry.get("openai-model")
    assert service.providers.for_model(m).name == "mock" and service.providers._real_factory is None


def test_real_calls_require_a_global_daily_budget():
    with pytest.raises(ProviderConfigError) as exc:
        build_chat_service(real_settings(budget_daily_usd=0), registry=all_provider_registry())
    assert "BUDGET_DAILY_USD" in str(exc.value)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_enabled_model_without_its_key_refuses_to_start(provider):
    s = real_settings(**{f"{provider}_api_key": None})
    issues = check_provider_configuration(s, all_provider_registry())
    assert [i.setting for i in issues] == [SUPPORTED_PROVIDERS[provider].key_setting]
    text = " ".join(str(i) for i in issues)
    assert not any(k in text for k in FAKE_KEYS.values())  # names the setting, never a value


@pytest.mark.parametrize("change, fragment", [
    ({"pricing_is_placeholder": True}, "pricing_is_placeholder"),
    ({"input_cost_per_million": 0.0}, "zero pricing"),
    ({"provider_model": "anthropic/claude-haiku-5-5"}, "must start with 'openai/'"),
    ({"provider": "acme", "provider_model": "acme/x"}, "unsupported provider"),
])
def test_unsafe_registry_entries_refuse_to_start(change, fragment):
    bad = real_model("openai").model_copy(update=change)
    issues = check_provider_configuration(real_settings(), ModelRegistry([bad]))
    assert any(fragment in str(i) for i in issues), issues


def test_disabled_and_mock_models_are_not_checked():
    reg = ModelRegistry([real_model("openai", enabled=False, pricing_is_placeholder=True), *load_registry(None).all()])
    assert check_provider_configuration(real_settings(openai_api_key=None), reg) == []


def test_create_app_refuses_unsafe_real_configuration():
    with pytest.raises(ProviderConfigError):
        create_app(real_settings(allow_real_provider_calls=False))


# =========================================================================== #
# 2. The verified provider registry
# =========================================================================== #
def test_provider_example_registry_is_verified_and_disabled():
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))["models"]
    reg = load_registry(EXAMPLE)
    assert reg.enabled() == []  # nothing is enabled by default
    assert {m.provider for m in reg.all()} == set(PROVIDERS)  # all five providers covered
    for m, item in zip(reg.all(), raw, strict=True):
        assert m.provider in SUPPORTED_PROVIDERS and m.provider_model.startswith(f"{m.provider}/")
        assert item["pricing_source"].startswith("https://") and item["pricing_verified_on"]
        assert m.input_cost_per_million > 0 and m.output_cost_per_million > 0
        assert m.max_output_tokens <= 8192  # router cap bounds the worst-case cost per call
    placeholders = {m.id for m in reg.all() if m.pricing_is_placeholder}
    assert placeholders == {"mistral-small-2603"}  # Mistral price labels could not be verified


def test_provider_example_registry_passes_startup_check_when_enabled_except_unverified_pricing():
    reg = load_registry(EXAMPLE)
    enabled = ModelRegistry(m.model_copy(update={"enabled": True}) for m in reg.all())
    issues = check_provider_configuration(real_settings(), enabled)
    assert [str(i) for i in issues] == [
        "MODEL_REGISTRY_FILE: model 'mistral-small-2603': pricing_is_placeholder=true; configure verified pricing first"
    ]


def test_long_context_pricing_tier_is_applied():
    from app.routing.usage import TokenUsage, estimate_cost

    haiku = load_registry(EXAMPLE).get("claude-haiku-5-5")
    short = estimate_cost(TokenUsage(100_000, 1_000, 101_000, "provider_reported"), haiku)
    long = estimate_cost(TokenUsage(100_001, 1_000, 101_001, "provider_reported"), haiku)
    assert (short.input_cost_per_million, short.output_cost_per_million) == (0.10, 0.50)
    assert (long.input_cost_per_million, long.output_cost_per_million) == (0.50, 2.50)


# =========================================================================== #
# 3. Per-provider request shape (dependency-injected fake client)
# =========================================================================== #
@pytest.mark.parametrize("provider", PROVIDERS)
def test_each_provider_gets_its_own_key_model_and_limits(provider):
    fake = FakeCompletion()
    out = chat(service_for(fake, real_max_output_tokens=256), model=f"{provider}-model")
    (call,) = fake.calls
    assert call["model"] == PROVIDER_MODELS[provider]
    assert call["api_key"] == FAKE_KEYS[provider]  # never another provider's key
    assert call["num_retries"] == 0  # retries are owned by the router
    assert call["timeout"] == 5.0
    assert call["max_tokens"] == 256  # REAL_MAX_OUTPUT_TOKENS cap
    assert out.provider == provider and out.provider_adapter == "litellm" and out.mock_mode is False
    assert out.usage.source == "provider_reported" and (out.usage.input_tokens, out.usage.output_tokens) == (12, 30)
    assert out.cost.total_cost == pytest.approx((12 * 1.0 + 30 * 2.0) / 1e6)


# =========================================================================== #
# 4. Error mapping per provider (LiteLLM's own exception classes, no network)
# =========================================================================== #
ERROR_CASES = [
    # litellm class, expected kind, calls with PROVIDER_MAX_RETRIES=1
    ("RateLimitError", ProviderErrorKind.RATE_LIMIT, 2),
    ("ServiceUnavailableError", ProviderErrorKind.UNAVAILABLE, 2),
    ("InternalServerError", ProviderErrorKind.UNAVAILABLE, 2),
    ("APIConnectionError", ProviderErrorKind.CONNECTION, 2),
    ("Timeout", ProviderErrorKind.TIMEOUT, 1),
    ("AuthenticationError", ProviderErrorKind.AUTH, 1),
    ("NotFoundError", ProviderErrorKind.NOT_FOUND, 1),
    ("BadRequestError", ProviderErrorKind.BAD_REQUEST, 1),
    ("ContextWindowExceededError", ProviderErrorKind.CONTEXT_WINDOW, 1),
    ("ContentPolicyViolationError", ProviderErrorKind.CONTENT_POLICY, 1),
]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("exc_name, kind, calls", ERROR_CASES)
def test_provider_errors_are_mapped_and_retried_per_policy(lx, provider, exc_name, kind, calls):
    exc = getattr(lx, exc_name)("simulated failure", provider, PROVIDER_MODELS[provider])
    fake = FakeCompletion({PROVIDER_MODELS[provider]: [exc]})
    with pytest.raises(AllModelsFailedError) as err:
        chat(service_for(fake), model=f"{provider}-model")  # override: no fallback
    assert len(fake.calls) == calls
    attempts = err.value.details["attempts"]
    assert {a["error_kind"] for a in attempts} == {kind}
    expected_code = "request_rejected_by_provider" if kind == ProviderErrorKind.CONTENT_POLICY else "all_models_failed"
    assert err.value.code == expected_code


@pytest.mark.parametrize("provider", PROVIDERS)
def test_transient_error_then_success_is_retried_on_same_provider(lx, provider):
    pm = PROVIDER_MODELS[provider]
    fake = FakeCompletion({pm: [lx.RateLimitError("slow down", provider, pm), "ok"]})
    out = chat(service_for(fake), model=f"{provider}-model")
    assert len(fake.calls) == 2 and out.model_used == f"{provider}-model" and not out.fallback.used
    assert [a.outcome for a in out.fallback.attempts] == ["error", "success"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_router_timeout_is_enforced_per_provider_and_not_retried(provider):
    pm = PROVIDER_MODELS[provider]
    reg = ModelRegistry([real_model(provider, timeout=0.05)])
    fake = FakeCompletion({pm: [("sleep", 2.0)]})
    with pytest.raises(AllModelsFailedError) as err:
        chat(service_for(fake, registry=reg), model=f"{provider}-model")
    assert len(fake.calls) == 1  # timeouts move on instead of doubling the wait
    assert err.value.details["attempts"][0]["error_kind"] == ProviderErrorKind.TIMEOUT
    assert fake.calls[0]["timeout"] == 0.05  # the provider SDK gets the same per-model timeout


def test_empty_or_malformed_provider_response_is_invalid_response():
    async def empty(**_):
        return {"choices": [{"message": {"content": "  "}}]}

    provider = LiteLLMProvider({"openai": None}, completion_fn=empty)  # type: ignore[dict-item]
    req = ProviderRequest("m", "openai", "openai/gpt-6-luna", ({"role": "user", "content": "hi"},), 10, 1.0)
    with pytest.raises(Exception) as exc:
        asyncio.run(provider.complete(req))
    assert getattr(exc.value, "kind", None) == ProviderErrorKind.INVALID_RESPONSE


# =========================================================================== #
# 5. Fallback and health tracking ACROSS providers
# =========================================================================== #
def test_fallback_walks_across_all_providers_in_order(lx):
    fake = FakeCompletion({PROVIDER_MODELS[p]: [lx.ServiceUnavailableError("down", p, PROVIDER_MODELS[p])]
                           for p in ("openai", "anthropic", "gemini", "groq")})
    out = chat(service_for(fake, max_fallback_models=4, provider_max_retries=0))
    assert out.selected_model == "openai-model" and out.model_used == "mistral-model" and out.fallback.used
    assert [c["model"] for c in fake.calls] == [PROVIDER_MODELS[p] for p in PROVIDERS]
    assert [c["api_key"] for c in fake.calls] == [FAKE_KEYS[p] for p in PROVIDERS]


def test_content_policy_refusal_is_not_shopped_to_other_providers(lx):
    pm = PROVIDER_MODELS["openai"]
    fake = FakeCompletion({pm: [lx.ContentPolicyViolationError("refused", "openai", pm)]})
    with pytest.raises(AllModelsFailedError) as err:
        chat(service_for(fake))
    assert len(fake.calls) == 1 and err.value.code == "request_rejected_by_provider"


def test_auth_failure_falls_back_to_another_provider(lx):
    pm = PROVIDER_MODELS["openai"]
    fake = FakeCompletion({pm: [lx.AuthenticationError("bad key", "openai", pm)]})
    out = chat(service_for(fake))
    assert out.model_used == "anthropic-model" and len(fake.calls) == 2


def test_failing_provider_circuit_opens_and_routing_moves_to_next_provider(lx):
    pm = PROVIDER_MODELS["openai"]
    fake = FakeCompletion({pm: [lx.ServiceUnavailableError("down", "openai", pm)]})
    service = service_for(fake, health_failure_threshold=2, health_cooldown_seconds=600)
    first = chat(service)  # openai fails twice (1 retry) -> circuit opens -> fallback to anthropic
    assert first.model_used == "anthropic-model" and first.fallback.used
    openai_calls = len(fake.calls_for(pm))
    second = chat(service)
    assert second.selected_model == "anthropic-model" and not second.fallback.used  # routed around it
    assert len(fake.calls_for(pm)) == openai_calls  # the open circuit is not called again
    view = asyncio.run(service.health.view(["openai-model", "anthropic-model"]))
    assert not view.is_available("openai-model") and view.is_available("anthropic-model")
    assert view.state("openai-model") == "unhealthy" and view.snapshot("openai-model").consecutive_failures >= 2


def test_bad_request_does_not_mark_provider_unhealthy(lx):
    pm = PROVIDER_MODELS["openai"]
    fake = FakeCompletion({pm: [lx.BadRequestError("bad param", "openai", pm)]})
    service = service_for(fake, health_failure_threshold=1)
    chat(service)
    view = asyncio.run(service.health.view(["openai-model"]))
    assert view.is_available("openai-model")


# =========================================================================== #
# 6. Keys never leak (logs, metrics, traces, error responses)
# =========================================================================== #
def test_provider_keys_never_leak_anywhere(monkeypatch, caplog, lx, tmp_path):
    key = FAKE_KEYS["openai"]
    pm = PROVIDER_MODELS["openai"]
    script = {
        pm: [lx.AuthenticationError(f"Incorrect API key provided: {key}", "openai", pm)],
        PROVIDER_MODELS["anthropic"]: [RuntimeError(f"unexpected failure with key {FAKE_KEYS['anthropic']}")],
        PROVIDER_MODELS["gemini"]: [lx.APIError(500, f"upstream echoed api_key={FAKE_KEYS['gemini']}", "gemini",
                                                PROVIDER_MODELS["gemini"])],
    }
    fake = FakeCompletion(script)
    monkeypatch.setattr("app.providers.litellm_provider._load_acompletion", lambda: fake)
    registry_file = tmp_path / "registry.json"
    reg = all_provider_registry(("openai", "anthropic", "gemini"))
    registry_file.write_text(json.dumps({"models": [json.loads(m.model_dump_json()) for m in reg.all()]}),
                             encoding="utf-8")
    settings = real_settings(model_registry_file=registry_file, tracing_enabled=True, tracing_exporter="memory",
                             provider_max_retries=0, log_level="DEBUG")
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(settings), raise_server_exceptions=False) as c:
        r = c.post("/api/v1/chat", json={"query": Q})
        metrics = c.get("/metrics").text
        spans = c.app.state.container.tracing.memory_exporter.get_finished_spans()
        models = c.get("/api/v1/models").text
    assert r.status_code == 503 and len(fake.calls) == 3  # all three providers really failed
    span_text = " ".join(f"{s.name} {dict(s.attributes or {})} {[e.attributes for e in s.events]}" for s in spans)
    for secret in FAKE_KEYS.values():
        for where, text in (("response", r.text), ("logs", caplog.text), ("metrics", metrics),
                            ("traces", span_text), ("models", models)):
            assert secret not in text, f"provider key leaked into {where}"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_provider_error_messages_are_redacted_at_the_source(lx, provider):
    """Defence in depth: the normalised error is clean even before the log filter runs."""
    from pydantic import SecretStr

    from app.providers.base import ProviderError

    pm, key = PROVIDER_MODELS[provider], FAKE_KEYS[provider]

    async def echoing_client(**kwargs):
        raise lx.AuthenticationError(f"invalid api key {kwargs['api_key']}", provider, pm)

    adapter = LiteLLMProvider({provider: SecretStr(key)}, completion_fn=echoing_client)
    req = ProviderRequest("m", provider, pm, ({"role": "user", "content": "hi"},), 10, 1.0)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(adapter.complete(req))
    assert exc.value.kind == ProviderErrorKind.AUTH
    assert key not in exc.value.message and key not in str(exc.value)


def test_open_circuit_recovers_after_cooldown_with_a_single_probe(lx):
    """Step 8 audit: a provider that comes back is routed to again (half-open -> healthy)."""
    pm = PROVIDER_MODELS["openai"]
    fake = FakeCompletion({pm: [lx.ServiceUnavailableError("down", "openai", pm),
                                lx.ServiceUnavailableError("down", "openai", pm), "ok"]})
    service = service_for(fake, health_failure_threshold=2, health_cooldown_seconds=0.05)
    first = chat(service)
    assert first.model_used == "anthropic-model"  # circuit opened, fallback served
    tracker = service.policy.health  # process-local tracker (memory backend)
    assert tracker.state("openai-model") == "unhealthy"
    import time

    time.sleep(0.1)  # cooldown elapses -> half-open: one probe is allowed
    second = chat(service)
    assert second.model_used == "openai-model" and not second.fallback.used
    view = asyncio.run(service.health.view(["openai-model"]))
    assert view.state("openai-model") == "healthy" and view.snapshot("openai-model").consecutive_failures == 0


# The dotenv-isolation regression tests live in tests/test_env_isolation.py (step 8 audit).
