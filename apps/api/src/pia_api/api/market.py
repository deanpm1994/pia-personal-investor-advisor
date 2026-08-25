"""Authenticated ISIN watchlist and portfolio analysis-candidate API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, StringConstraints

from pia_api.core.auth import AuthenticatedUser, get_authenticated_user
from pia_api.services.market_watchlist import MarketWatchlistError, WatchlistMutation

router = APIRouter(prefix="/v1/market")


class WatchlistAddRequest(BaseModel):
    isin: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]


class ResolutionCandidateResponse(BaseModel):
    mic: str
    quote_currency: str
    provider: str
    provider_symbol: str


class InstrumentResponse(BaseModel):
    instrument_id: str
    isin: str
    share_class_figi: str | None
    instrument_kind: str
    display_name: str
    mic: str
    quote_currency: str
    provider: str
    provider_symbol: str


class WatchlistEntryResponse(InstrumentResponse):
    id: str
    added_at: datetime


class WatchlistMutationResponse(BaseModel):
    status: Literal[
        "added",
        "duplicate",
        "invalid",
        "unsupported",
        "ambiguous",
        "temporarily_unavailable",
        "provider_disabled",
    ]
    action: str
    entry: WatchlistEntryResponse | None
    candidates: list[ResolutionCandidateResponse]


class PortfolioCandidateResponse(BaseModel):
    source_instrument_id: str
    source_kind: Literal["confirmed", "observed", "mixed"]
    quantity: str
    evidence_event_ids: list[str]
    snapshot_id: str
    snapshot_as_of: datetime | None
    snapshot_refreshed_at: datetime
    coverage_status: Literal["supported", "unresolved", "unsupported_source_identity"]
    instrument: InstrumentResponse | None
    action: str


class MarketWatchlistGateway(Protocol):
    async def list_entries(
        self, user: AuthenticatedUser
    ) -> list[dict[str, object]]: ...

    async def add(
        self, user: AuthenticatedUser, isin: str
    ) -> WatchlistMutation | dict[str, object]: ...

    async def remove(self, user: AuthenticatedUser, entry_id: str) -> bool: ...

    async def list_portfolio_candidates(
        self, user: AuthenticatedUser
    ) -> list[dict[str, object]]: ...


def _gateway(request: Request) -> MarketWatchlistGateway:
    gateway = getattr(request.app.state, "market_watchlist_gateway", None)
    if gateway is None:
        raise HTTPException(status_code=503, detail="Market watchlist is unavailable")
    return gateway


@router.get("/watchlist", response_model=list[WatchlistEntryResponse])
async def list_watchlist(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[MarketWatchlistGateway, Depends(_gateway)],
) -> list[dict[str, object]]:
    return await gateway.list_entries(user)


@router.post("/watchlist", response_model=WatchlistMutationResponse)
async def add_to_watchlist(
    command: WatchlistAddRequest,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[MarketWatchlistGateway, Depends(_gateway)],
) -> WatchlistMutation | dict[str, object]:
    try:
        return await gateway.add(user, command.isin)
    except MarketWatchlistError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.delete("/watchlist/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_watchlist(
    entry_id: str,
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[MarketWatchlistGateway, Depends(_gateway)],
) -> Response:
    if not await gateway.remove(user, entry_id):
        raise HTTPException(status_code=404, detail="Watchlist entry not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/portfolio-candidates", response_model=list[PortfolioCandidateResponse])
async def list_portfolio_candidates(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[MarketWatchlistGateway, Depends(_gateway)],
) -> list[dict[str, object]]:
    return await gateway.list_portfolio_candidates(user)
