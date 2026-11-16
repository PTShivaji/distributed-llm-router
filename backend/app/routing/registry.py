"""Model registry: validated, configurable model definitions.

Models are loaded from JSON (never hardcoded in business logic). Pricing is
data, and each entry declares whether its pricing is a placeholder.
"""

import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.classifier.types import ComplexityLevel

MODEL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"
DEFAULT_REGISTRY_FILE = Path(__file__).with_name("default_models.json")


class RegistryError(ValueError):
    """Raised for an invalid registry configuration (fails fast at startup)."""


class ModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=MODEL_ID_PATTERN, description="Stable id used by clients and routing.")
    provider: str = Field(min_length=1, max_length=50, description="Provider name, e.g. 'openai', 'mock'.")
    provider_model: str = Field(min_length=1, max_length=200, description="Identifier sent to the provider.")
    tier: ComplexityLevel
    input_cost_per_million: float = Field(ge=0, description="USD per 1M input tokens.")
    output_cost_per_million: float = Field(ge=0, description="USD per 1M output tokens.")
    pricing_is_placeholder: bool = Field(default=True, description="True unless real pricing was configured.")
    max_context_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600, description="None -> global default.")
    enabled: bool = True
    capabilities: frozenset[str] = frozenset({"chat"})
    priority: int = Field(default=100, ge=0, description="Lower = preferred (priority strategy).")
    fallbacks: tuple[str, ...] = Field(default=(), description="Ordered fallback model ids.")
    api_base: str | None = Field(default=None, description="Optional custom endpoint (e.g. local Ollama).")
    description: str = ""
    # --- Phase 5 step 8: pricing provenance + prompt-length pricing tiers (all optional) ---
    pricing_source: str | None = Field(default=None, max_length=500, description="Official pricing page URL.")
    pricing_verified_on: date | None = Field(default=None, description="Date the pricing was checked.")
    # Some providers charge more when the PROMPT exceeds a size (e.g. Claude Haiku 5.5
    # above 100,000 tokens, OpenAI GPT-6 models above 272K). Applied per request.
    long_context_threshold_tokens: int | None = Field(default=None, gt=0)
    long_context_input_cost_per_million: float | None = Field(default=None, ge=0)
    long_context_output_cost_per_million: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check(self) -> "ModelConfig":
        long_fields = (self.long_context_threshold_tokens, self.long_context_input_cost_per_million,
                       self.long_context_output_cost_per_million)
        if any(v is not None for v in long_fields) and any(v is None for v in long_fields):
            raise ValueError("long_context_* pricing fields must be set together")
        if self.long_context_input_cost_per_million is not None and (
            self.long_context_input_cost_per_million < self.input_cost_per_million
            or self.long_context_output_cost_per_million < self.output_cost_per_million  # type: ignore[operator]
        ):
            raise ValueError("long-context prices cannot be lower than the base prices")
        if self.max_output_tokens > self.max_context_tokens:
            raise ValueError("max_output_tokens cannot exceed max_context_tokens")
        if self.id in self.fallbacks:
            raise ValueError("a model cannot list itself as a fallback")
        if len(set(self.fallbacks)) != len(self.fallbacks):
            raise ValueError("duplicate fallback ids")
        return self

    @property
    def blended_cost_per_million(self) -> float:
        return self.input_cost_per_million + self.output_cost_per_million

    def rates_for(self, input_tokens: int | None) -> tuple[float, float]:
        """(input, output) USD per 1M tokens for a request with this many prompt tokens."""
        if (self.long_context_threshold_tokens is not None and input_tokens is not None
                and input_tokens > self.long_context_threshold_tokens):
            return self.long_context_input_cost_per_million, self.long_context_output_cost_per_million  # type: ignore[return-value]
        return self.input_cost_per_million, self.output_cost_per_million


class ModelRegistry:
    def __init__(self, models: Iterable[ModelConfig]) -> None:
        self._models: dict[str, ModelConfig] = {}
        for m in models:
            if m.id in self._models:
                raise RegistryError(f"duplicate model id '{m.id}'")
            self._models[m.id] = m
        if not self._models:
            raise RegistryError("registry must contain at least one model")
        for m in self._models.values():
            unknown = [f for f in m.fallbacks if f not in self._models]
            if unknown:
                raise RegistryError(f"model '{m.id}' has unknown fallback(s): {', '.join(unknown)}")

    # -- construction -------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelRegistry":
        raw = data.get("models")
        if not isinstance(raw, list):
            raise RegistryError("registry JSON must contain a 'models' list")
        try:
            return cls(ModelConfig.model_validate(item) for item in raw)
        except ValidationError as exc:
            raise RegistryError(f"invalid model definition: {exc}") from exc

    @classmethod
    def from_file(cls, path: Path) -> "ModelRegistry":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RegistryError(f"model registry file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise RegistryError(f"model registry file is not valid JSON: {path}: {exc}") from exc
        return cls.from_dict(data)

    # -- queries ------------------------------------------------------------
    def get(self, model_id: str) -> ModelConfig | None:
        return self._models.get(model_id)

    def all(self) -> list[ModelConfig]:
        return list(self._models.values())

    def enabled(self, tier: ComplexityLevel | None = None) -> list[ModelConfig]:
        return [m for m in self._models.values() if m.enabled and (tier is None or m.tier == tier)]

    def __contains__(self, model_id: object) -> bool:
        return model_id in self._models

    def __len__(self) -> int:
        return len(self._models)


def load_registry(path: Path | None) -> ModelRegistry:
    return ModelRegistry.from_file(path or DEFAULT_REGISTRY_FILE)
