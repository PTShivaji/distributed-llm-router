"""GET /models - registry contents and live per-model health (no secrets)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.chat import get_chat_service
from app.routing.service import ChatService
from app.schemas.chat import ModelHealthOut, ModelOut, ModelsResponse

router = APIRouter(tags=["models"])


@router.get("/models", response_model=ModelsResponse, summary="List registered models and their health")
async def list_models(service: Annotated[ChatService, Depends(get_chat_service)]) -> ModelsResponse:
    default_timeout = service.execution.default_timeout_seconds
    all_models = service.policy.registry.all()
    view = await service.health.view([m.id for m in all_models])
    models = [
        ModelOut(
            id=m.id,
            provider=m.provider,
            provider_model=m.provider_model,
            tier=m.tier,
            enabled=m.enabled,
            capabilities=sorted(m.capabilities),
            priority=m.priority,
            input_cost_per_million=m.input_cost_per_million,
            output_cost_per_million=m.output_cost_per_million,
            pricing_is_placeholder=m.pricing_is_placeholder,
            max_context_tokens=m.max_context_tokens,
            max_output_tokens=m.max_output_tokens,
            timeout_seconds=m.timeout_seconds or default_timeout,
            fallbacks=list(m.fallbacks),
            description=m.description,
            health=ModelHealthOut.model_validate(view.snapshot(m.id)),
        )
        for m in all_models
    ]
    return ModelsResponse(
        mock_mode=service.providers.mock_mode, routing_strategy=service.policy.strategy,
        health_backend=service.health.backend, models=models,
    )
