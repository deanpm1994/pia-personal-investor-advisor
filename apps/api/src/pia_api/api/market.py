"""Authenticated ISIN watchlist and portfolio analysis-candidate API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from pia_api.core.auth import AuthenticatedUser, get_authenticated_user
from pia_api.services.market_analysis import MarketAnalysisError
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


class DailyBarResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_date: date
    open: str
    high: str
    low: str
    close: str
    volume: int | None
    revision: int
    provider_as_of: datetime
    retrieved_at: datetime
    source_url: str
    completeness_status: Literal["complete", "incomplete"]
    corrected: bool


class IndicatorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal["sma_20", "sma_50", "sma_200", "rsi_14"]
    market_date: date
    value: str | None
    status: Literal["available", "insufficient_history"]
    observation_count: int
    required_observations: int
    window_start: date
    window_end: date
    provider_as_of: datetime
    retrieved_at: datetime
    source_urls: list[str]
    freshness_status: Literal["fresh", "pending", "stale", "unavailable"]
    completeness_status: Literal["complete", "incomplete", "unavailable"]
    corrected: bool
    diagnostics: list[dict[str, object]]


class MarketSourceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    provider_symbol: str
    mic: str
    quote_currency: str
    attribution: str
    source_urls: list[str]
    provider_as_of: datetime
    retrieved_at: datetime


class AnalysisFreshnessResponse(BaseModel):
    status: Literal["fresh", "pending", "stale", "unavailable"]


class AnalysisCompletenessResponse(BaseModel):
    status: Literal["complete", "incomplete", "unavailable"]


class AnalysisPositionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: str
    evidence_event_ids: list[str]
    snapshot_id: str
    snapshot_as_of: datetime | None
    snapshot_refreshed_at: datetime
    snapshot_input_fingerprint: str


class NativeValuationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "available",
        "no_position",
        "market_data_unavailable",
        "basis_unavailable",
        "currency_mismatch",
        "quantity_mismatch",
    ]
    quote_currency: str
    current_price: str | None = None
    current_value: str | None = None
    total_basis: str | None = None
    unrealized_gain: str | None = None
    unrealized_return_percent: str | None = None
    evidence_event_ids: list[str]


class AnalysisDiagnosticResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    market_date: date | None = None
    evidence_event_ids: list[str] = Field(default_factory=list)


class InstrumentAnalysisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: Literal["watchlist", "portfolio", "portfolio_and_watchlist"]
    source_instrument_id: str
    state: Literal[
        "ready",
        "incomplete",
        "stale",
        "unavailable",
        "unsupported",
        "provider_disabled",
        "license_review_required",
    ]
    instrument: InstrumentResponse | None
    bars: list[DailyBarResponse]
    indicators: list[IndicatorResponse]
    source: MarketSourceResponse | None
    freshness: AnalysisFreshnessResponse
    completeness: AnalysisCompletenessResponse
    position: AnalysisPositionResponse | None
    valuation: NativeValuationResponse | None
    diagnostics: list[AnalysisDiagnosticResponse]


class MarketAnalysisCollectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["ready", "empty"]
    items: list[InstrumentAnalysisResponse]


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


class MarketAnalysisGateway(Protocol):
    async def list_analysis(
        self, user: AuthenticatedUser
    ) -> list[dict[str, object]]: ...


def _gateway(request: Request) -> MarketWatchlistGateway:
    gateway = getattr(request.app.state, "market_watchlist_gateway", None)
    if gateway is None:
        raise HTTPException(status_code=503, detail="Market watchlist is unavailable")
    return gateway


def _analysis_gateway(request: Request) -> MarketAnalysisGateway:
    gateway = getattr(request.app.state, "market_analysis_gateway", None)
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "MARKET_ANALYSIS_UNAVAILABLE",
                "message": "Market analysis is unavailable",
            },
        )
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


@router.get("/analysis", response_model=MarketAnalysisCollectionResponse)
async def list_market_analysis(
    user: Annotated[AuthenticatedUser, Depends(get_authenticated_user)],
    gateway: Annotated[MarketAnalysisGateway, Depends(_analysis_gateway)],
) -> MarketAnalysisCollectionResponse:
    """Read persisted owner analysis without refreshing a provider or snapshot."""
    try:
        items = await gateway.list_analysis(user)
    except MarketAnalysisError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "MARKET_ANALYSIS_UNAVAILABLE",
                "message": "Market analysis is unavailable",
            },
        ) from error
    return MarketAnalysisCollectionResponse(
        state="ready" if items else "empty",
        items=items,
    )
