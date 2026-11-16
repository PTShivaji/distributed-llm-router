"""Aggregates all versioned API routers under /api/v1."""

from fastapi import APIRouter

from app.api import analytics, budget, cache, chat, classify, health, models, settings

api_v1_router = APIRouter()
api_v1_router.include_router(health.router)
api_v1_router.include_router(classify.router)
api_v1_router.include_router(chat.router)
api_v1_router.include_router(models.router)
api_v1_router.include_router(cache.router)
api_v1_router.include_router(analytics.router)
api_v1_router.include_router(settings.router)
api_v1_router.include_router(budget.router)
