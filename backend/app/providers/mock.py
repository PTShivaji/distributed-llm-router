"""Deterministic mock provider for local development and automated tests.

- Same request -> same response (no randomness, no timestamps).
- Never calls the network and needs no API keys.
- Can simulate failures per model id to exercise retry/fallback paths.
- Does NOT invent token counts by default: usage is reported as unknown, so
  the router labels it "estimated". `report_usage=True` simulates a provider
  that reports usage (word counts), for testing that code path.
"""

import asyncio
import hashlib
from collections import Counter
from collections.abc import Mapping

from app.providers.base import (
    LLMProvider,
    MockFailureMode,
    ProviderError,
    ProviderErrorKind,
    ProviderRequest,
    ProviderResponse,
)

_FAILURE_KINDS: dict[str, ProviderErrorKind] = {
    "unavailable": ProviderErrorKind.UNAVAILABLE,
    "rate_limit": ProviderErrorKind.RATE_LIMIT,
    "connection": ProviderErrorKind.CONNECTION,
    "auth": ProviderErrorKind.AUTH,
    "bad_request": ProviderErrorKind.BAD_REQUEST,
    "content_policy": ProviderErrorKind.CONTENT_POLICY,
}


class MockProvider(LLMProvider):
    name = "mock"

    def __init__(
        self,
        failures: Mapping[str, MockFailureMode] | None = None,
        *,
        latency_ms: int = 0,
        report_usage: bool = False,
    ) -> None:
        self.failures = dict(failures or {})
        self.latency_ms = latency_ms
        self.report_usage = report_usage
        self.calls: Counter[str] = Counter()

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls[request.model_id] += 1
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)

        mode = self.failures.get(request.model_id)
        if mode == "timeout":
            # Genuinely exceed the deadline so the caller's timeout handling is exercised.
            await asyncio.sleep(request.timeout_seconds + 5)
        elif mode == "flaky" and self.calls[request.model_id] % 2 == 1:
            raise ProviderError(ProviderErrorKind.UNAVAILABLE, "mock flaky failure (odd call)", provider=self.name)
        elif mode in _FAILURE_KINDS:
            raise ProviderError(_FAILURE_KINDS[mode], f"mock simulated '{mode}' failure", provider=self.name)

        prompt = "\n".join(m["content"] for m in request.messages)
        fingerprint = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        content = (
            f"[MOCK:{request.model_id}] Deterministic mock answer for a "
            f"{len(prompt.split())}-word prompt (fingerprint {fingerprint}). "
            "No external model was called. Set LLM_MOCK_MODE=false and configure a "
            "model registry to use real providers."
        )
        return ProviderResponse(
            content=content,
            provider_model=request.provider_model,
            input_tokens=len(prompt.split()) if self.report_usage else None,
            output_tokens=len(content.split()) if self.report_usage else None,
            finish_reason="stop",
        )
