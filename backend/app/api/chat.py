"""POST /chat - authenticate (CHAT_AUTH_MODE), rate limit, classify, route, cache, call with retry/fallback."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.deps import enforce_chat_access
from app.routing.service import ChatCommand, ChatService
from app.schemas.chat import ChatRequest, ChatResponse

router = APIRouter(tags=["chat"])


def get_chat_service(request: Request) -> ChatService:
    """The service is created once per app (create_app) so shared state persists."""
    return request.app.state.chat_service


@router.post("/chat", response_model=ChatResponse, summary="Route a query to the best model and answer it")
async def chat(
    body: ChatRequest,
    request: Request,
    service: Annotated[ChatService, Depends(get_chat_service)],
    client_id: Annotated[str, Depends(enforce_chat_access)],
) -> ChatResponse:
    outcome = await service.chat(
        ChatCommand(
            query=body.query,
            request_id=request.state.request_id,
            model_override=body.model,
            temperature=body.temperature,
            max_output_tokens=body.max_output_tokens,
            client_id=client_id,
            use_cache=body.use_cache,
        )
    )
    return ChatResponse.model_validate(outcome)
