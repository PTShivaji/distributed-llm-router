"""POST /classify - rule-based query classification endpoint."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.classifier import QueryClassifier, Thresholds
from app.core.config import Settings, get_settings
from app.schemas.classification import ClassifyRequest, ClassifyResponse

router = APIRouter(tags=["classification"])


def get_classifier(settings: Annotated[Settings, Depends(get_settings)]) -> QueryClassifier:
    """Build the classifier from settings. Cheap: all regexes are precompiled at import."""
    return QueryClassifier(
        Thresholds(
            simple_max=settings.classifier_simple_threshold,
            complex_min=settings.classifier_complex_threshold,
        )
    )


@router.post(
    "/classify",
    response_model=ClassifyResponse,
    summary="Classify a query as SIMPLE, MEDIUM or COMPLEX",
)
async def classify(
    request: ClassifyRequest,
    classifier: Annotated[QueryClassifier, Depends(get_classifier)],
) -> ClassifyResponse:
    result = classifier.classify(request.query)
    return ClassifyResponse.model_validate(result)
