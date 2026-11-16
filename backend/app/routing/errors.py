"""Routing errors. All use the standard {"error": {...}} envelope via AppError."""

from typing import Any

from fastapi import status

from app.core.errors import AppError


class UnknownModelError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "unknown_model"


class ModelDisabledError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "model_disabled"


class NoModelAvailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "no_model_available"


class AllModelsFailedError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "all_models_failed"

    def __init__(self, message: str, *, details: Any = None, rejected: bool = False) -> None:
        super().__init__(message, details=details)
        if rejected:  # e.g. content policy: the request itself was refused
            self.status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
            self.code = "request_rejected_by_provider"


class BudgetExceededError(AppError):
    """Every candidate real-provider call was refused by a spend safeguard."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "budget_exceeded"


class BudgetUnavailableError(AppError):
    """Budgets are configured but their store (Redis) is unreachable (BUDGET_FAIL_CLOSED=true)."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "budget_unavailable"
