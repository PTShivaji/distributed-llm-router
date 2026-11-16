"""Chooses which provider adapter serves a given model."""

from collections.abc import Callable

from app.providers.base import LLMProvider
from app.providers.mock import MockProvider
from app.routing.registry import ModelConfig


class ProviderPool:
    """mock mode -> always MockProvider; otherwise provider 'mock' -> mock, anything else -> LiteLLM."""

    def __init__(
        self,
        *,
        mock: MockProvider,
        mock_mode: bool,
        real_factory: Callable[[], LLMProvider] | None = None,
    ) -> None:
        self.mock = mock
        self.mock_mode = mock_mode
        self._real_factory = real_factory
        self._real: LLMProvider | None = None

    def for_model(self, model: ModelConfig) -> LLMProvider:
        if self.mock_mode or model.provider == "mock":
            return self.mock
        if self._real is None:
            if self._real_factory is None:
                raise RuntimeError("no real provider configured")
            self._real = self._real_factory()
        return self._real
