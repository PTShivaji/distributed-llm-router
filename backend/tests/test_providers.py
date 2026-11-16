"""Provider abstraction: mock provider, LiteLLM adapter (no network), pool."""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.providers.base import ERROR_POLICIES, ProviderError, ProviderErrorKind, ProviderRequest
from app.providers.litellm_provider import LiteLLMProvider, map_litellm_exception, parse_completion
from app.providers.mock import MockProvider
from app.providers.pool import ProviderPool
from tests.conftest import model


def req(model_id: str = "m", **kw) -> ProviderRequest:
    base = dict(
        model_id=model_id, provider="openai", provider_model="openai/test-model",
        messages=({"role": "user", "content": "Explain Python variables"},),
        max_output_tokens=100, timeout_seconds=5.0,
    )
    base.update(kw)
    return ProviderRequest(**base)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Error policy table
# --------------------------------------------------------------------------- #
def test_every_error_kind_has_a_policy():
    assert set(ERROR_POLICIES) == set(ProviderErrorKind)


@pytest.mark.parametrize(
    "kind, retryable, fallback",
    [
        (ProviderErrorKind.RATE_LIMIT, True, True),
        (ProviderErrorKind.UNAVAILABLE, True, True),
        (ProviderErrorKind.TIMEOUT, False, True),
        (ProviderErrorKind.AUTH, False, True),
        (ProviderErrorKind.BAD_REQUEST, False, True),
        (ProviderErrorKind.CONTENT_POLICY, False, False),
    ],
)
def test_error_policy(kind, retryable, fallback):
    e = ProviderError(kind, "x", provider="p")
    assert (e.retryable, e.fallback_allowed) == (retryable, fallback)


# --------------------------------------------------------------------------- #
# Mock provider
# --------------------------------------------------------------------------- #
def test_mock_is_deterministic():
    p = MockProvider()
    a, b = run(p.complete(req())), run(p.complete(req()))
    assert a == b
    assert a.content.startswith("[MOCK:m]")
    assert a.finish_reason == "stop"


def test_mock_different_prompts_differ():
    p = MockProvider()
    other = req(messages=({"role": "user", "content": "Something else"},))
    assert run(p.complete(req())).content != run(p.complete(other)).content


def test_mock_does_not_invent_usage_by_default():
    r = run(MockProvider().complete(req()))
    assert r.input_tokens is None and r.output_tokens is None


def test_mock_can_report_usage():
    r = run(MockProvider(report_usage=True).complete(req()))
    assert r.input_tokens == 3 and r.output_tokens > 0


@pytest.mark.parametrize(
    "mode, kind",
    [
        ("unavailable", ProviderErrorKind.UNAVAILABLE),
        ("rate_limit", ProviderErrorKind.RATE_LIMIT),
        ("connection", ProviderErrorKind.CONNECTION),
        ("auth", ProviderErrorKind.AUTH),
        ("bad_request", ProviderErrorKind.BAD_REQUEST),
        ("content_policy", ProviderErrorKind.CONTENT_POLICY),
    ],
)
def test_mock_failure_modes(mode, kind):
    with pytest.raises(ProviderError) as info:
        run(MockProvider({"m": mode}).complete(req()))
    assert info.value.kind == kind


def test_mock_failure_only_affects_configured_model():
    p = MockProvider({"broken": "unavailable"})
    assert run(p.complete(req("healthy"))).content


def test_mock_flaky_alternates():
    p = MockProvider({"m": "flaky"})
    with pytest.raises(ProviderError):
        run(p.complete(req()))
    assert run(p.complete(req())).content
    assert p.calls["m"] == 2


def test_mock_timeout_mode_really_exceeds_deadline():
    async def go():
        return await asyncio.wait_for(MockProvider({"m": "timeout"}).complete(req(timeout_seconds=0.05)), 0.05)

    with pytest.raises(TimeoutError):
        run(go())


# --------------------------------------------------------------------------- #
# LiteLLM adapter (fake completion function: no network, no keys)
# --------------------------------------------------------------------------- #
def fake_response(content="Hello!", prompt_tokens=12, completion_tokens=3, usage=True):
    return SimpleNamespace(
        model="openai/test-model-2026",
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens) if usage else None,
    )


def test_litellm_success_and_kwargs():
    seen = {}

    async def fn(**kwargs):
        seen.update(kwargs)
        return fake_response()

    p = LiteLLMProvider({"openai": SecretStr("sk-test-secret-123456")}, completion_fn=fn)
    r = run(p.complete(req(temperature=0.3, api_base="http://localhost:1")))

    assert (r.content, r.input_tokens, r.output_tokens) == ("Hello!", 12, 3)
    assert r.provider_model == "openai/test-model-2026"
    assert seen["model"] == "openai/test-model"
    assert seen["num_retries"] == 0  # router owns retries
    assert seen["max_tokens"] == 100 and seen["timeout"] == 5.0
    assert seen["temperature"] == 0.3 and seen["api_base"] == "http://localhost:1"
    assert seen["api_key"] == "sk-test-secret-123456"


def test_litellm_no_key_for_provider_is_not_sent():
    seen = {}

    async def fn(**kwargs):
        seen.update(kwargs)
        return fake_response()

    run(LiteLLMProvider({}, completion_fn=fn).complete(req()))
    assert "api_key" not in seen and "temperature" not in seen


def test_litellm_missing_usage_is_none_not_zero():
    async def fn(**_):
        return fake_response(usage=False)

    r = run(LiteLLMProvider(completion_fn=fn).complete(req()))
    assert r.input_tokens is None and r.output_tokens is None


@pytest.mark.parametrize("response", [SimpleNamespace(choices=[]), fake_response(content=""), fake_response(content=None)])
def test_litellm_invalid_response(response):
    with pytest.raises(ProviderError) as info:
        parse_completion(response, provider="openai", fallback_model="x")
    assert info.value.kind == ProviderErrorKind.INVALID_RESPONSE


def test_litellm_parses_dict_responses():
    r = parse_completion(
        {"choices": [{"message": {"content": "hi"}, "finish_reason": "length"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
        provider="openai", fallback_model="x",
    )
    assert (r.content, r.input_tokens, r.output_tokens, r.finish_reason, r.provider_model) == ("hi", 1, 2, "length", "x")


def test_litellm_timeout_error_mapped():
    async def fn(**_):
        raise TimeoutError

    with pytest.raises(ProviderError) as info:
        run(LiteLLMProvider(completion_fn=fn).complete(req()))
    assert info.value.kind == ProviderErrorKind.TIMEOUT


def test_litellm_error_message_redacts_api_key():
    secret = "sk-live-abcdefghijklmnop"

    async def fn(**_):
        raise RuntimeError(f"Incorrect API key provided: {secret}")

    with pytest.raises(ProviderError) as info:
        run(LiteLLMProvider({"openai": SecretStr(secret)}, completion_fn=fn).complete(req()))
    assert secret not in info.value.message
    assert "***" in info.value.message


@pytest.mark.parametrize(
    "status, kind",
    [(408, "timeout"), (429, "rate_limit"), (401, "auth"), (404, "not_found"), (400, "bad_request"), (503, "unavailable"), (None, "unknown")],
)
def test_status_code_mapping(status, kind):
    exc = RuntimeError("boom")
    exc.status_code = status
    assert map_litellm_exception(exc, provider="p", secrets=[]).kind == kind


def test_real_litellm_exception_classes_are_mapped():
    """Uses the installed LiteLLM exception types (import only; no network)."""
    litellm = pytest.importorskip("litellm")
    cases = {
        litellm.Timeout(message="t", model="m", llm_provider="openai"): "timeout",
        litellm.RateLimitError(message="r", model="m", llm_provider="openai"): "rate_limit",
        litellm.AuthenticationError(message="a", model="m", llm_provider="openai"): "auth",
        litellm.ContextWindowExceededError(message="c", model="m", llm_provider="openai"): "context_window",
        litellm.ContentPolicyViolationError(message="p", model="m", llm_provider="openai"): "content_policy",
        litellm.BadRequestError(message="b", model="m", llm_provider="openai"): "bad_request",
        litellm.ServiceUnavailableError(message="s", model="m", llm_provider="openai"): "unavailable",
        litellm.APIConnectionError(message="n", model="m", llm_provider="openai"): "connection",
    }
    for exc, kind in cases.items():
        assert map_litellm_exception(exc, provider="openai", secrets=[]).kind == kind, type(exc).__name__


# --------------------------------------------------------------------------- #
# Provider pool
# --------------------------------------------------------------------------- #
def test_pool_mock_mode_always_mock():
    mock = MockProvider()
    pool = ProviderPool(mock=mock, mock_mode=True, real_factory=lambda: pytest.fail("must not build real provider"))
    assert pool.for_model(model("x", provider="openai")) is mock


def test_pool_real_mode_routes_by_provider():
    mock, real = MockProvider(), LiteLLMProvider(completion_fn=lambda **_: None)
    built = []
    pool = ProviderPool(mock=mock, mock_mode=False, real_factory=lambda: built.append(1) or real)
    assert pool.for_model(model("m")) is mock  # provider == "mock"
    assert pool.for_model(model("o", provider="openai")) is real
    assert pool.for_model(model("a", provider="anthropic")) is real
    assert built == [1]  # created lazily, once
