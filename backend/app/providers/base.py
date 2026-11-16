"""Provider abstraction: common interface, request/response types and errors.

This module has no dependencies on the rest of the app, so every provider
(mock, LiteLLM, future native SDKs) shares exactly the same contract.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

Message = dict[str, str]  # {"role": "user", "content": "..."}

MockFailureMode = Literal[
    "timeout", "unavailable", "rate_limit", "connection", "auth", "bad_request", "content_policy", "flaky"
]


class ProviderErrorKind(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    UNAVAILABLE = "unavailable"
    CONNECTION = "connection"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    CONTEXT_WINDOW = "context_window"
    CONTENT_POLICY = "content_policy"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ErrorPolicy:
    retryable: bool  # retry the SAME model?
    fallback_allowed: bool  # try a DIFFERENT model?
    affects_health: bool  # count against the model's health?


# Central, explicit failure-handling table.
#  - Transient errors (rate limit, 5xx, connection) are retried.
#  - Timeouts are NOT retried on the same model (it is likely slow right now);
#    we move to a fallback instead of doubling the user's wait.
#  - Permanent errors (auth, bad request, not found) are never retried.
#  - Content-policy refusals do not fall back: we don't shop a refused request around.
#  - Request-shaped errors (bad request, context window) don't mark the model unhealthy.
ERROR_POLICIES: dict[ProviderErrorKind, ErrorPolicy] = {
    ProviderErrorKind.TIMEOUT: ErrorPolicy(retryable=False, fallback_allowed=True, affects_health=True),
    ProviderErrorKind.RATE_LIMIT: ErrorPolicy(retryable=True, fallback_allowed=True, affects_health=True),
    ProviderErrorKind.UNAVAILABLE: ErrorPolicy(retryable=True, fallback_allowed=True, affects_health=True),
    ProviderErrorKind.CONNECTION: ErrorPolicy(retryable=True, fallback_allowed=True, affects_health=True),
    ProviderErrorKind.AUTH: ErrorPolicy(retryable=False, fallback_allowed=True, affects_health=True),
    ProviderErrorKind.NOT_FOUND: ErrorPolicy(retryable=False, fallback_allowed=True, affects_health=True),
    ProviderErrorKind.BAD_REQUEST: ErrorPolicy(retryable=False, fallback_allowed=True, affects_health=False),
    ProviderErrorKind.CONTEXT_WINDOW: ErrorPolicy(retryable=False, fallback_allowed=True, affects_health=False),
    ProviderErrorKind.CONTENT_POLICY: ErrorPolicy(retryable=False, fallback_allowed=False, affects_health=False),
    ProviderErrorKind.INVALID_RESPONSE: ErrorPolicy(retryable=False, fallback_allowed=True, affects_health=True),
    ProviderErrorKind.UNKNOWN: ErrorPolicy(retryable=False, fallback_allowed=True, affects_health=True),
}


class ProviderError(Exception):
    """Normalised provider failure. `message` must be safe to log (no secrets)."""

    def __init__(
        self, kind: ProviderErrorKind, message: str, *, provider: str, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.provider = provider
        self.status_code = status_code

    @property
    def policy(self) -> ErrorPolicy:
        return ERROR_POLICIES[self.kind]

    @property
    def retryable(self) -> bool:
        return self.policy.retryable

    @property
    def fallback_allowed(self) -> bool:
        return self.policy.fallback_allowed

    @property
    def affects_health(self) -> bool:
        return self.policy.affects_health


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    model_id: str  # registry id, e.g. "mock-fast"
    provider: str  # registry provider name, e.g. "openai"
    provider_model: str  # identifier sent to the provider, e.g. "openai/gpt-4o-mini"
    messages: tuple[Message, ...]
    max_output_tokens: int
    timeout_seconds: float
    temperature: float | None = None
    api_base: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    content: str
    provider_model: str
    input_tokens: int | None  # None = provider did not report it
    output_tokens: int | None
    finish_reason: str | None = None


class LLMProvider(ABC):
    """Common interface implemented by every provider adapter."""

    name: str

    @abstractmethod
    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return a completion or raise ProviderError. Never return partial garbage."""
