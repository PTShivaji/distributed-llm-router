"""LiteLLM-backed provider adapter.

- `litellm` is imported lazily on first use (the import takes several seconds),
  so mock mode and the test suite never pay for it.
- LiteLLM's own retries are disabled (`num_retries=0`): retry/fallback is
  owned by the router so the behaviour is explicit and traceable.
- LiteLLM exceptions are mapped to ProviderErrorKind by class name (walking the
  MRO, most specific first), then by HTTP status code.
- Error messages are redacted before they are stored or logged.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import SecretStr

from app.core.logging import redact
from app.providers.base import LLMProvider, ProviderError, ProviderErrorKind, ProviderRequest, ProviderResponse

CompletionFn = Callable[..., Awaitable[Any]]

_NAME_TO_KIND: dict[str, ProviderErrorKind] = {
    "Timeout": ProviderErrorKind.TIMEOUT,
    "APITimeoutError": ProviderErrorKind.TIMEOUT,
    "RateLimitError": ProviderErrorKind.RATE_LIMIT,
    "ContextWindowExceededError": ProviderErrorKind.CONTEXT_WINDOW,
    "ContentPolicyViolationError": ProviderErrorKind.CONTENT_POLICY,
    "AuthenticationError": ProviderErrorKind.AUTH,
    "PermissionDeniedError": ProviderErrorKind.AUTH,
    "PaymentRequiredError": ProviderErrorKind.AUTH,
    "NotFoundError": ProviderErrorKind.NOT_FOUND,
    "BadRequestError": ProviderErrorKind.BAD_REQUEST,
    "UnprocessableEntityError": ProviderErrorKind.BAD_REQUEST,
    "UnsupportedParamsError": ProviderErrorKind.BAD_REQUEST,
    "ServiceUnavailableError": ProviderErrorKind.UNAVAILABLE,
    "InternalServerError": ProviderErrorKind.UNAVAILABLE,
    "BadGatewayError": ProviderErrorKind.UNAVAILABLE,
    "APIConnectionError": ProviderErrorKind.CONNECTION,
}


def _kind_from_status(status: int | None) -> ProviderErrorKind:
    if status is None:
        return ProviderErrorKind.UNKNOWN
    if status == 408:
        return ProviderErrorKind.TIMEOUT
    if status == 429:
        return ProviderErrorKind.RATE_LIMIT
    if status in (401, 402, 403):
        return ProviderErrorKind.AUTH
    if status == 404:
        return ProviderErrorKind.NOT_FOUND
    if status in (400, 413, 422):
        return ProviderErrorKind.BAD_REQUEST
    if status >= 500:
        return ProviderErrorKind.UNAVAILABLE
    return ProviderErrorKind.UNKNOWN


def map_litellm_exception(exc: BaseException, *, provider: str, secrets: list[str]) -> ProviderError:
    status = getattr(exc, "status_code", None)
    status = status if isinstance(status, int) else None
    kind = next(
        (_NAME_TO_KIND[cls.__name__] for cls in type(exc).__mro__ if cls.__name__ in _NAME_TO_KIND),
        None,
    ) or _kind_from_status(status)
    message = redact(f"{type(exc).__name__}: {exc}", secrets)
    return ProviderError(kind, message, provider=provider, status_code=status)


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def parse_completion(response: Any, *, provider: str, fallback_model: str) -> ProviderResponse:
    try:
        choice = _field(response, "choices")[0]
        content = _field(_field(choice, "message"), "content")
    except (TypeError, IndexError, KeyError) as exc:
        raise ProviderError(ProviderErrorKind.INVALID_RESPONSE, "response had no choices/message", provider=provider) from exc
    if not isinstance(content, str) or not content.strip():
        raise ProviderError(ProviderErrorKind.INVALID_RESPONSE, "response content was empty", provider=provider)

    usage = _field(response, "usage")
    return ProviderResponse(
        content=content,
        provider_model=_field(response, "model") or fallback_model,
        input_tokens=_as_int(_field(usage, "prompt_tokens")) if usage is not None else None,
        output_tokens=_as_int(_field(usage, "completion_tokens")) if usage is not None else None,
        finish_reason=_field(choice, "finish_reason"),
    )


def _load_acompletion() -> CompletionFn:
    from app.providers.litellm_env import import_litellm  # noqa: PLC0415

    # never load_dotenv() / fetch the remote cost map on import (see litellm_env)
    return import_litellm().acompletion


class LiteLLMProvider(LLMProvider):
    name = "litellm"

    def __init__(
        self, api_keys: Mapping[str, SecretStr] | None = None, *, completion_fn: CompletionFn | None = None
    ) -> None:
        self._api_keys = dict(api_keys or {})
        self._completion_fn = completion_fn

    def _secrets(self) -> list[str]:
        return [k.get_secret_value() for k in self._api_keys.values()]

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        if self._completion_fn is None:
            self._completion_fn = _load_acompletion()

        kwargs: dict[str, Any] = {
            "model": request.provider_model,
            "messages": list(request.messages),
            "max_tokens": request.max_output_tokens,
            "timeout": request.timeout_seconds,
            "num_retries": 0,
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.api_base:
            kwargs["api_base"] = request.api_base
        if key := self._api_keys.get(request.provider):
            kwargs["api_key"] = key.get_secret_value()

        try:
            response = await self._completion_fn(**kwargs)
        except ProviderError:
            raise
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ProviderError(
                ProviderErrorKind.TIMEOUT, f"timed out after {request.timeout_seconds}s", provider=request.provider
            ) from exc
        except Exception as exc:  # mapped, never swallowed
            raise map_litellm_exception(exc, provider=request.provider, secrets=self._secrets()) from exc

        return parse_completion(response, provider=request.provider, fallback_model=request.provider_model)
