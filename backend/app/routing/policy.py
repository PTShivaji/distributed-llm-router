"""Routing policy: classification -> model, plus the ordered fallback chain.

Selection, in order:
  1. Explicit override: must be registered AND enabled, else rejected.
  2. Tier search: classified tier first; with escalation, then higher tiers
     (more capable) and finally lower tiers.
  3. Within a tier keep enabled models that fit the context window, prefer
     healthy ones, rank by (missing preferred capabilities, strategy key).
  4. If every candidate is unhealthy, use the best one anyway rather than fail.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from app.classifier.types import ClassificationResult, ComplexityLevel
from app.routing.errors import ModelDisabledError, NoModelAvailableError, UnknownModelError
from app.routing.health import HealthTracker
from app.routing.registry import ModelConfig, ModelRegistry

Strategy = Literal["priority", "lowest_cost"]


class HealthLike(Protocol):
    def is_available(self, model_id: str) -> bool: ...
TIERS = (ComplexityLevel.SIMPLE, ComplexityLevel.MEDIUM, ComplexityLevel.COMPLEX)


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    model: ModelConfig
    requested_tier: ComplexityLevel
    routed_tier: ComplexityLevel
    strategy: str
    override: bool
    reason: str
    candidates: tuple[str, ...]
    preferred_capabilities: tuple[str, ...]

    @property
    def selected_model(self) -> str:
        return self.model.id


def preferred_capabilities(c: ClassificationResult, estimated_input_tokens: int) -> tuple[str, ...]:
    caps = []
    if c.technical.is_coding:
        caps.append("coding")
    if c.complexity == ComplexityLevel.COMPLEX:
        caps.append("reasoning")
    if estimated_input_tokens > 8000:
        caps.append("long_context")
    return tuple(caps)


def tier_search_order(tier: ComplexityLevel, escalate: bool) -> list[ComplexityLevel]:
    if not escalate:
        return [tier]
    i = TIERS.index(tier)
    return [tier, *TIERS[i + 1 :], *reversed(TIERS[:i])]


def fits(model: ModelConfig, estimated_input_tokens: int, max_output_tokens: int) -> bool:
    return estimated_input_tokens + min(max_output_tokens, model.max_output_tokens) <= model.max_context_tokens


class RoutingPolicy:
    def __init__(
        self,
        registry: ModelRegistry,
        health: HealthTracker,
        *,
        strategy: Strategy = "priority",
        tier_escalation: bool = True,
        fallback_auto_tier: bool = True,
        max_fallback_models: int = 3,
    ) -> None:
        self.registry = registry
        self.health = health
        self.strategy = strategy
        self.tier_escalation = tier_escalation
        self.fallback_auto_tier = fallback_auto_tier
        self.max_fallback_models = max_fallback_models

    # ------------------------------------------------------------------ #
    def resolve_override(self, model_id: str) -> ModelConfig:
        model = self.registry.get(model_id)
        if model is None:
            raise UnknownModelError(f"Model '{model_id}' is not registered.")
        if not model.enabled:
            raise ModelDisabledError(f"Model '{model_id}' is disabled.")
        return model

    def _rank(self, models: list[ModelConfig], prefer: tuple[str, ...]) -> list[ModelConfig]:
        def key(m: ModelConfig) -> tuple:
            missing = sum(1 for cap in prefer if cap not in m.capabilities)
            primary = m.blended_cost_per_million if self.strategy == "lowest_cost" else m.priority
            return (missing, primary, m.priority, m.id)

        return sorted(models, key=key)

    # ------------------------------------------------------------------ #
    def select(
        self,
        classification: ClassificationResult,
        *,
        override: str | None = None,
        estimated_input_tokens: int = 0,
        max_output_tokens: int = 1024,
        health: "HealthLike | None" = None,
    ) -> RoutingDecision:
        """`health` is an optional per-request snapshot (e.g. read from Redis);
        defaults to the policy's own tracker."""
        health = health or self.health
        tier = classification.complexity
        prefer = preferred_capabilities(classification, estimated_input_tokens)
        header = f"Query classified as {tier} (confidence {classification.confidence:.2f})."

        if override:
            model = self.resolve_override(override)
            return RoutingDecision(
                model=model, requested_tier=tier, routed_tier=model.tier, strategy="override", override=True,
                reason=f"{header} Explicit override: '{model.id}' ({model.tier} tier) was requested and is enabled.",
                candidates=(model.id,), preferred_capabilities=prefer,
            )

        notes: list[str] = []
        unhealthy_pool: list[tuple[ComplexityLevel, ModelConfig]] = []
        for t in tier_search_order(tier, self.tier_escalation):
            candidates = [m for m in self.registry.enabled(t) if fits(m, estimated_input_tokens, max_output_tokens)]
            healthy = [m for m in candidates if health.is_available(m.id)]
            unhealthy_pool += [(t, m) for m in candidates if m not in healthy]
            if not healthy:
                notes.append(
                    f"No healthy enabled model in {t} tier"
                    + (f" ({len(candidates)} unhealthy)" if candidates else "")
                    + "."
                )
                continue
            ranked = self._rank(healthy, prefer)
            return self._decision(ranked[0], tier, t, ranked, prefer, header, notes)

        if unhealthy_pool:  # better to try an unhealthy model than to refuse outright
            t, _ = unhealthy_pool[0]
            ranked = self._rank([m for tt, m in unhealthy_pool if tt == t], prefer)
            notes.append("All candidates are currently unhealthy; trying the best one anyway.")
            return self._decision(ranked[0], tier, t, ranked, prefer, header, notes)

        raise NoModelAvailableError(
            f"No enabled model can serve a {tier} query.",
            details={"requested_tier": tier, "tier_escalation": self.tier_escalation},
        )

    def _decision(self, model, tier, routed, ranked, prefer, header, notes) -> RoutingDecision:
        parts = [header]
        if routed != tier:
            parts.append(f"Routed to {routed} tier instead of {tier}.")
        parts += notes
        parts.append(
            f"Selected '{model.id}' using the '{self.strategy}' strategy among "
            f"{len(ranked)} eligible model(s) in {routed} tier."
        )
        if prefer:
            have = [c for c in prefer if c in model.capabilities]
            parts.append(f"Preferred capabilities: {', '.join(prefer)}; model has: {', '.join(have) or 'none'}.")
        return RoutingDecision(
            model=model, requested_tier=tier, routed_tier=routed, strategy=self.strategy, override=False,
            reason=" ".join(parts), candidates=tuple(m.id for m in ranked), preferred_capabilities=prefer,
        )

    # ------------------------------------------------------------------ #
    def fallback_chain(
        self,
        primary: ModelConfig,
        classification: ClassificationResult,
        *,
        estimated_input_tokens: int = 0,
        max_output_tokens: int = 1024,
    ) -> list[ModelConfig]:
        """Ordered, de-duplicated, capped list of alternatives (never contains primary).

        Being finite and duplicate-free is what makes infinite fallback loops impossible.
        """
        prefer = preferred_capabilities(classification, estimated_input_tokens)
        seen = {primary.id}
        chain: list[ModelConfig] = []

        def add(m: ModelConfig | None) -> None:
            if m and m.enabled and m.id not in seen and fits(m, estimated_input_tokens, max_output_tokens):
                seen.add(m.id)
                chain.append(m)

        for fid in primary.fallbacks:
            add(self.registry.get(fid))
        if self.fallback_auto_tier:
            for t in tier_search_order(primary.tier, escalate=True):
                for m in self._rank(self.registry.enabled(t), prefer):
                    add(m)
        return chain[: self.max_fallback_models]
