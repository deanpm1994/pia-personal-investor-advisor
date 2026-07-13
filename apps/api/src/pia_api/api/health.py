"""Public health endpoint."""

from typing import Literal, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel

from pia_api.core.config import Environment, Settings

router = APIRouter()


class HealthResponse(BaseModel):
    """Deterministic response returned by the health endpoint."""

    status: Literal["ok"]
    environment: Environment


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    """Report that the credential-free API shell is available."""
    settings = cast(Settings, request.app.state.settings)
    return HealthResponse(status="ok", environment=settings.environment)
