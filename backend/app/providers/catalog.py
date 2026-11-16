"""Supported real providers and the startup check that gates paid calls.

Real provider calls happen only when ALL of these hold (checked at startup;
any failure refuses to start, in every environment):

  * LLM_MOCK_MODE=false AND ALLOW_REAL_PROVIDER_CALLS=true (double opt-in)
  * BUDGET_DAILY_USD > 0 (a hard global daily spend limit)
  * every ENABLED non-mock model:
      - uses a supported provider and a matching LiteLLM prefix ("<provider>/...")
      - has that provider's API key configured
      - has real pricing (pricing_is_placeholder=false, non-zero prices
        except for local providers such as Ollama)

Messages name settings and model ids only - never key values.
"""

import math
from dataclasses import dataclass
from urllib.parse import urlparse

from app.core.config import Settings
from app.core.production import ConfigIssue
from app.routing.registry import ModelRegistry


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    key_setting: str | None  # environment variable holding the API key (None = no key)
    local: bool = False  # runs on infrastructure you operate (zero marginal price allowed)
    docs: str = ""


# LiteLLM routes "<prefix>/<model>" to the provider; prefix == registry provider name.
SUPPORTED_PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec("openai", "OPENAI_API_KEY", docs="https://developers.openai.com/api/docs/models"),
    "anthropic": ProviderSpec("anthropic", "ANTHROPIC_API_KEY",
                              docs="https://platform.claude.com/docs/en/about-claude/models/overview"),
    "gemini": ProviderSpec("gemini", "GEMINI_API_KEY", docs="https://aistudio.google.com/docs/models"),
    "groq": ProviderSpec("groq", "GROQ_API_KEY", docs="https://console.groq.com/docs/models"),
    "mistral": ProviderSpec("mistral", "MISTRAL_API_KEY", docs="https://docs.mistral.ai/getting-started/models/"),
    "ollama": ProviderSpec("ollama", None, local=True, docs="https://ollama.com/library"),
}


class ProviderConfigError(RuntimeError):
    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = issues
        lines = "\n".join(f"  - {i}" for i in issues)
        super().__init__(f"Refusing to start: unsafe real-provider configuration ({len(issues)} problem(s)):\n{lines}")


def check_provider_configuration(s: Settings, registry: ModelRegistry) -> list[ConfigIssue]:
    """Errors that must block startup when mock mode is off. Empty list in mock mode."""
    if s.llm_mock_mode:
        return []
    issues: list[ConfigIssue] = []

    def err(setting: str, message: str) -> None:
        issues.append(ConfigIssue(setting, "error", message))

    if not s.allow_real_provider_calls:
        err("ALLOW_REAL_PROVIDER_CALLS",
            "LLM_MOCK_MODE=false also requires ALLOW_REAL_PROVIDER_CALLS=true (explicit opt-in to paid calls)")
    if s.budget_daily_usd <= 0:
        err("BUDGET_DAILY_USD", "a global daily spend limit (> 0 USD) is required for real provider calls")

    keys = s.provider_api_keys()
    for m in registry.enabled():
        if m.provider == "mock":
            continue
        spec = SUPPORTED_PROVIDERS.get(m.provider)
        where = f"model '{m.id}'"
        if spec is None:
            err("MODEL_REGISTRY_FILE", f"{where}: unsupported provider '{m.provider}' "
                                       f"(supported: {', '.join(sorted(SUPPORTED_PROVIDERS))})")
            continue
        if not m.provider_model.startswith(f"{m.provider}/"):
            err("MODEL_REGISTRY_FILE", f"{where}: provider_model must start with '{m.provider}/' so the request "
                                       "is routed to the provider whose key is used")
        if spec.key_setting and m.provider not in keys:
            err(spec.key_setting, f"{where} is enabled but {spec.key_setting} is not set")
        if m.pricing_is_placeholder:
            err("MODEL_REGISTRY_FILE", f"{where}: pricing_is_placeholder=true; configure verified pricing first")
        elif not spec.local and (m.input_cost_per_million <= 0 or m.output_cost_per_million <= 0):
            err("MODEL_REGISTRY_FILE", f"{where}: zero pricing for a paid provider would disable budget enforcement")
    return issues


def check_embedding_configuration(s: Settings) -> list[ConfigIssue]:
    """Step 8A startup gate for paid embeddings. Applies in EVERY mode, including mock mode.

    The local hashing embedder (default) is free and is never checked here.
    """
    if s.embedding_provider != "litellm":
        return []
    issues: list[ConfigIssue] = []

    def err(setting: str, message: str) -> None:
        issues.append(ConfigIssue(setting, "error", message))

    if not s.allow_real_provider_calls:
        err("ALLOW_REAL_PROVIDER_CALLS", "EMBEDDING_PROVIDER=litellm makes paid calls and requires "
                                         "ALLOW_REAL_PROVIDER_CALLS=true (also when LLM_MOCK_MODE=true)")
    if s.budget_daily_usd <= 0:
        err("BUDGET_DAILY_USD", "a global daily spend limit (> 0 USD) is required for paid embeddings")

    provider, sep, model = s.embedding_model.partition("/")
    spec = SUPPORTED_PROVIDERS.get(provider) if sep and model else None
    if spec is None or spec.local:
        err("EMBEDDING_MODEL", "must be '<provider>/<model>' with a supported paid provider "
                               f"({', '.join(sorted(n for n, p in SUPPORTED_PROVIDERS.items() if not p.local))})")
    elif spec.key_setting and provider not in s.provider_api_keys():
        err(spec.key_setting, f"EMBEDDING_PROVIDER=litellm uses '{provider}' but {spec.key_setting} is not set")

    price = s.embedding_input_cost_per_million
    if s.embedding_pricing_is_placeholder:
        err("EMBEDDING_PRICING_IS_PLACEHOLDER", "set to false only after verifying the official embedding price")
    if not (math.isfinite(price) and price > 0):
        err("EMBEDDING_INPUT_COST_PER_MILLION", "a verified, positive USD price per 1M input tokens is required")
    source = urlparse(s.embedding_pricing_source or "")
    if source.scheme != "https" or not source.netloc:
        err("EMBEDDING_PRICING_SOURCE", "the official pricing page URL (https://...) is required")
    return issues


def enforce_embedding_configuration(s: Settings) -> None:
    issues = check_embedding_configuration(s)
    if issues:
        raise ProviderConfigError(issues)


def enforce_provider_configuration(s: Settings, registry: ModelRegistry) -> None:
    issues = check_provider_configuration(s, registry)
    if issues:
        raise ProviderConfigError(issues)
