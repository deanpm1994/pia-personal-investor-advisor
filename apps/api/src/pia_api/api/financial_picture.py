"""Authenticated read and explicit refresh endpoints for financial snapshots."""

from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from pia_api.core.auth import AuthenticatedUser, get_authenticated_user
from pia_api.services.financial_snapshots import (
    SnapshotReadError,
    SnapshotReadResult,
    SnapshotRefreshError,
    SnapshotRefreshResult,
)

router = APIRouter()


class FreshnessResponse(BaseModel):
    status: Literal["fresh", "stale"]


class CompletenessResponse(BaseModel):
    status: Literal["complete", "incomplete"]
    diagnostic_count: int


class FinancialPictureResponse(BaseModel):
    """Traceable persisted accounting facts; financial scalars remain strings."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    input_fingerprint: str
    as_of: str | None
    refreshed_at: str
    input_counts: dict[str, int]
    state: Literal["ready", "no_ledger_data", "incomplete", "stale"]
    freshness: FreshnessResponse
    completeness: CompletenessResponse
    refresh_reused: bool | None = None
    account_summaries: list[dict[str, object]]
    cash_by_currency: dict[str, object]
    positions: dict[str, object]
    fifo: dict[str, object]
    reserve_progress: dict[str, object]
    diagnostics: list[dict[str, object]]
    evidence_event_ids: list[str]


class FinancialPictureGateway(Protocol):
    async def refresh(self, user: AuthenticatedUser) -> SnapshotRefreshResult: ...

    async def get_latest(
        self, user: AuthenticatedUser
    ) -> SnapshotReadResult | None: ...


def _gateway(request: Request) -> FinancialPictureGateway:
    gateway = getattr(request.app.state, "financial_picture_gateway", None)
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "FINANCIAL_PICTURE_UNAVAILABLE",
                "message": "Financial-picture snapshots are unavailable",
            },
        )
    return gateway


def _snapshot_or_not_found(
    snapshot: SnapshotReadResult | None,
) -> SnapshotReadResult:
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "FINANCIAL_PICTURE_NO_SNAPSHOT",
                "message": "No financial snapshot exists; refresh it explicitly",
            },
        )
    return snapshot


def _response(
    snapshot: SnapshotReadResult, *, refresh_reused: bool | None = None
) -> FinancialPictureResponse:
    _reject_binary_floats(snapshot.content)
    diagnostics = snapshot.content.get("diagnostics", [])
    if not isinstance(diagnostics, list):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "FINANCIAL_PICTURE_UNAVAILABLE",
                "message": "The persisted financial snapshot is invalid",
            },
        )
    reserve_progress = snapshot.content.get("reserve_progress", {})
    reserve_is_incomplete = (
        isinstance(reserve_progress, dict)
        and reserve_progress.get("status") == "incomplete"
    )
    incomplete = bool(diagnostics) or reserve_is_incomplete
    if not snapshot.is_fresh:
        picture_state = "stale"
    elif snapshot.input_counts.get("events", 0) == 0:
        picture_state = "no_ledger_data"
    elif incomplete:
        picture_state = "incomplete"
    else:
        picture_state = "ready"
    try:
        return FinancialPictureResponse(
            snapshot_id=snapshot.snapshot_id,
            input_fingerprint=snapshot.input_fingerprint,
            as_of=snapshot.as_of,
            refreshed_at=snapshot.refreshed_at,
            input_counts=snapshot.input_counts,
            state=picture_state,
            freshness={"status": "fresh" if snapshot.is_fresh else "stale"},
            completeness={
                "status": "incomplete" if incomplete else "complete",
                "diagnostic_count": len(diagnostics),
            },
            refresh_reused=refresh_reused,
            account_summaries=snapshot.content["account_summaries"],
            cash_by_currency=snapshot.content["cash_by_currency"],
            positions=snapshot.content["positions"],
            fifo=snapshot.content["fifo"],
            reserve_progress=reserve_progress,
            diagnostics=diagnostics,
            evidence_event_ids=snapshot.content["evidence_event_ids"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "FINANCIAL_PICTURE_UNAVAILABLE",
                "message": "The persisted financial snapshot is invalid",
            },
        ) from error


def _reject_binary_floats(value: object) -> None:
    """Never serialize an accidental binary float in a financial snapshot."""
    if isinstance(value, float):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "FINANCIAL_PICTURE_UNAVAILABLE",
                "message": "The persisted financial snapshot is invalid",
            },
        )
    if isinstance(value, dict):
        for item in value.values():
            _reject_binary_floats(item)
    elif isinstance(value, list):
        for item in value:
            _reject_binary_floats(item)


@router.get(
    "/v1/financial-picture",
    response_model=FinancialPictureResponse,
)
async def get_financial_picture(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[FinancialPictureGateway, Depends(_gateway)],
) -> FinancialPictureResponse:
    """Read only the authenticated owner's latest traceable snapshot."""
    try:
        snapshot = await gateway.get_latest(user)
    except SnapshotReadError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "FINANCIAL_PICTURE_UNAVAILABLE",
                "message": "Financial-picture snapshots are unavailable",
            },
        ) from error
    return _response(_snapshot_or_not_found(snapshot))


@router.post(
    "/v1/financial-picture/refresh",
    response_model=FinancialPictureResponse,
)
async def refresh_financial_picture(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[FinancialPictureGateway, Depends(_gateway)],
) -> FinancialPictureResponse:
    """Explicitly refresh the owner snapshot without provider or AI calls."""
    try:
        refresh = await gateway.refresh(user)
        snapshot = await gateway.get_latest(user)
    except SnapshotRefreshError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "FINANCIAL_PICTURE_REFRESH_FAILED",
                "message": "Financial-picture refresh failed; retry explicitly",
            },
        ) from error
    except SnapshotReadError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "FINANCIAL_PICTURE_UNAVAILABLE",
                "message": "Financial-picture snapshots are unavailable",
            },
        ) from error
    response = _response(
        _snapshot_or_not_found(snapshot), refresh_reused=refresh.reused
    )
    if response.snapshot_id != refresh.snapshot_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "FINANCIAL_PICTURE_UNAVAILABLE",
                "message": "Refreshed financial snapshot is unavailable",
            },
        )
    return response
