"""Minimal authenticated identity contract."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from pia_api.core.auth import AuthenticatedUser, get_authenticated_user

router = APIRouter()


class IdentityResponse(BaseModel):
    """Identity returned only after JWT verification."""

    id: str
    email: str | None


@router.get("/v1/identity", response_model=IdentityResponse)
async def get_identity(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
) -> IdentityResponse:
    """Prove the API authentication boundary without authorizing domain data."""
    return IdentityResponse(id=user.id, email=user.email)
