"""Real (meaning-aware) embeddings through LiteLLM's `aembedding`.

Not exercised against a live provider by the test suite (tests inject a fake
function). LiteLLM is imported lazily, like the chat adapter.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import SecretStr

from app.core.logging import redact
from app.embeddings.base import EmbeddingError, EmbeddingProvider, normalize
from app.providers.base import ProviderErrorKind
from app.providers.litellm_provider import map_litellm_exception

EmbeddingFn = Callable[..., Awaitable[Any]]


def _reported_input_tokens(response: Any) -> int | None:
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if usage is None:
        return None
    for name in ("prompt_tokens", "total_tokens"):
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _load_aembedding() -> EmbeddingFn:
    from app.providers.litellm_env import import_litellm  # noqa: PLC0415

    # never load_dotenv() / fetch the remote cost map on import (see litellm_env)
    return import_litellm().aembedding


class LiteLLMEmbedder(EmbeddingProvider):
    is_semantic = True

    def __init__(
        self,
        model: str,
        *,
        api_key: SecretStr | None = None,
        timeout_seconds: float = 10.0,
        embedding_fn: EmbeddingFn | None = None,
    ) -> None:
        if not model:
            raise ValueError("EMBEDDING_MODEL is required when EMBEDDING_PROVIDER=litellm")
        self.model = model
        self.id = f"litellm:{model}"
        self.dimensions = 0  # learned from the first response
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._fn = embedding_fn

    async def embed(self, text: str) -> list[float]:
        vector, _ = await self.embed_with_usage(text)
        return vector

    async def embed_with_usage(self, text: str) -> tuple[list[float], int | None]:
        """Returns the vector and the provider-reported input tokens (usage.prompt_tokens /
        total_tokens), or None when the provider reports no usage."""
        if self._fn is None:
            self._fn = _load_aembedding()
        kwargs: dict[str, Any] = {"model": self.model, "input": [text], "timeout": self._timeout}
        if self._api_key:
            kwargs["api_key"] = self._api_key.get_secret_value()
        secrets = [self._api_key.get_secret_value()] if self._api_key else []
        try:
            response = await asyncio.wait_for(self._fn(**kwargs), timeout=self._timeout)
        except TimeoutError as exc:
            raise EmbeddingError(f"timed out after {self._timeout}s", kind=ProviderErrorKind.TIMEOUT) from exc
        except Exception as exc:
            mapped = map_litellm_exception(exc, provider=self.model.split("/", 1)[0], secrets=secrets)
            raise EmbeddingError(mapped.message, kind=mapped.kind) from exc
        try:
            item = (response["data"] if isinstance(response, dict) else response.data)[0]
            vector = item["embedding"] if isinstance(item, dict) else item.embedding
        except Exception as exc:  # the call succeeded (and may be billed) but the shape is wrong
            raise EmbeddingError(redact(f"{type(exc).__name__}: malformed embedding response", secrets),
                                 kind=ProviderErrorKind.INVALID_RESPONSE) from exc
        if not vector:
            raise EmbeddingError("empty embedding returned", kind=ProviderErrorKind.INVALID_RESPONSE)
        self.dimensions = len(vector)
        return normalize([float(x) for x in vector]), _reported_input_tokens(response)
