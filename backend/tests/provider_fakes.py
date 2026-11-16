"""Test doubles for real-provider integration (Phase 5 step 8).

Nothing here touches the network: `FakeCompletion` replaces `litellm.acompletion`
through dependency injection, so no provider is contacted and nothing is billed.
The API keys below are obviously fake strings, never real credentials.
"""

import asyncio
from collections.abc import Iterable
from typing import Any

from app.core.config import Settings
from app.routing.registry import ModelConfig, ModelRegistry

FAKE_KEYS = {
    "openai": "fake-openai-key-for-tests-0001-zq",
    "anthropic": "fake-anthropic-key-for-tests-0002-zq",
    "gemini": "fake-gemini-key-for-tests-0003-zq",
    "groq": "fake-groq-key-for-tests-0004-zq",
    "mistral": "fake-mistral-key-for-tests-0005-zq",
}
PROVIDERS = tuple(FAKE_KEYS)

# One model per provider. Prices are TEST VALUES (not claims about real pricing).
PROVIDER_MODELS = {
    "openai": "openai/gpt-6-luna",
    "anthropic": "anthropic/claude-haiku-5-5",
    "gemini": "gemini/gemini-3.5-flash-lite",
    "groq": "groq/openai/gpt-oss-120b",
    "mistral": "mistral/mistral-small-2603",
}


def real_model(provider: str, *, model_id: str | None = None, tier: str = "SIMPLE", priority: int = 10,
               input_cost: float = 1.0, output_cost: float = 2.0, timeout: float | None = 5.0,
               fallbacks: Iterable[str] = (), max_output: int = 2048, enabled: bool = True,
               pricing_is_placeholder: bool = False, **extra: Any) -> ModelConfig:
    return ModelConfig(
        id=model_id or f"{provider}-model", provider=provider, provider_model=PROVIDER_MODELS[provider], tier=tier,
        input_cost_per_million=input_cost, output_cost_per_million=output_cost,
        pricing_is_placeholder=pricing_is_placeholder, max_context_tokens=100_000, max_output_tokens=max_output,
        timeout_seconds=timeout, enabled=enabled,
        priority=priority, fallbacks=tuple(fallbacks), **extra,
    )


def all_provider_registry(providers: Iterable[str] = PROVIDERS, **overrides: Any) -> ModelRegistry:
    """openai -> anthropic -> gemini -> groq -> mistral, all SIMPLE, chained as fallbacks."""
    providers = list(providers)
    ids = [f"{p}-model" for p in providers]
    return ModelRegistry(
        real_model(p, priority=i, fallbacks=ids[i + 1:], **overrides) for i, p in enumerate(providers)
    )


def real_settings(**overrides: Any) -> Settings:
    """Real-provider mode (both opt-ins + budget + fake keys); all I/O backends off."""
    base: dict[str, Any] = dict(
        _env_file=None, environment="test", log_level="WARNING", llm_mock_mode=False,
        allow_real_provider_calls=True, budget_daily_usd=10.0, provider_retry_backoff_seconds=0,
        provider_max_retries=1, redis_enabled=False, database_enabled=False, cache_enabled=False,
        rate_limit_enabled=False, health_backend="memory", mock_failures={},
        **{f"{p}_api_key": k for p, k in FAKE_KEYS.items()},
    )
    base.update(overrides)
    return Settings(**base)


def ok_response(model: str, prompt_tokens: int = 12, completion_tokens: int = 30) -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", "content": f"answer from {model}"}, "finish_reason": "stop"}],
        "model": model,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class FakeCompletion:
    """Scripted stand-in for litellm.acompletion.

    script: provider_model -> list of actions, consumed in order (the last one repeats):
        "ok"                      -> a normal response
        BaseException instance    -> raised
        ("sleep", seconds)        -> waits (to trigger the router's own timeout), then "ok"
        ("sleep_no_usage", s)     -> waits, then a response WITHOUT usage (with
                                     ESTIMATE_MISSING_USAGE=false the reservation is kept:
                                     deterministic budget arithmetic under concurrency)
        ("usage", in, out)        -> a response reporting this usage
    """

    def __init__(self, script: dict[str, list[Any]] | None = None) -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls: list[dict[str, Any]] = []

    def calls_for(self, provider_model: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["model"] == provider_model]

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        actions = self.script.get(kwargs["model"], ["ok"])
        action = actions.pop(0) if len(actions) > 1 else actions[0]
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, tuple) and action[0] == "sleep":
            await asyncio.sleep(action[1])
        if isinstance(action, tuple) and action[0] == "usage":
            return ok_response(kwargs["model"], action[1], action[2])
        if isinstance(action, tuple) and action[0] == "sleep_no_usage":
            await asyncio.sleep(action[1])
            response = ok_response(kwargs["model"])
            response.pop("usage")
            return response
        return ok_response(kwargs["model"])
