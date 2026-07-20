"""Authenticated staged-import upload and review contracts."""

from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from pia_api.core.auth import AuthenticatedUser, get_authenticated_user

router = APIRouter()


class DiagnosticResponse(BaseModel):
    code: str
    message: str


class RowReviewResponse(BaseModel):
    row_number: int
    events: list[dict[str, object]]
    diagnostics: list[DiagnosticResponse]


class ImportReviewResponse(BaseModel):
    id: str
    status: str
    row_count: int
    event_count: int
    diagnostic_count: int
    confirmation_eligible: bool
    rows: list[RowReviewResponse]


class StagedImportGateway(Protocol):
    async def stage(
        self, user: AuthenticatedUser, filename: str, content_type: str, content: bytes
    ) -> dict[str, object]: ...

    async def review(
        self, user: AuthenticatedUser, import_id: str
    ) -> dict[str, object] | None: ...


def _gateway(request: Request) -> StagedImportGateway:
    gateway = getattr(request.app.state, "import_gateway", None)
    if gateway is None:
        raise HTTPException(status_code=503, detail="Import staging is unavailable")
    return gateway


@router.post(
    "/v1/imports",
    response_model=ImportReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def stage_import(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[StagedImportGateway, Depends(_gateway)],
) -> dict[str, object]:
    """Stage a CSV using the authenticated owner's persistence boundary."""
    filename = request.headers.get("X-Import-Filename", "")
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if not filename.lower().endswith(".csv") or content_type != "text/csv":
        raise HTTPException(status_code=422, detail="Only CSV uploads are supported")
    content = await request.body()
    if not content:
        raise HTTPException(status_code=422, detail="CSV upload is empty")
    return await gateway.stage(user, filename, content_type, content)


@router.get("/v1/imports/{import_id}", response_model=ImportReviewResponse)
async def get_import_review(
    import_id: str,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[StagedImportGateway, Depends(_gateway)],
) -> dict[str, object]:
    """Return a normalized owner-scoped review without raw CSV contents."""
    review = await gateway.review(user, import_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Staged import not found")
    return review
