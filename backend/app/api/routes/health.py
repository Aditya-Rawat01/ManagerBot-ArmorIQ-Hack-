"""Liveness endpoint. Deliberately independent of every external service."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app import __version__
from app.api.deps import get_app_settings
from app.core.config import Settings
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness check")
def health(settings: Annotated[Settings, Depends(get_app_settings)]) -> HealthResponse:
    return HealthResponse(
        service=settings.service_name,
        version=__version__,
        environment=settings.app_env,
    )
