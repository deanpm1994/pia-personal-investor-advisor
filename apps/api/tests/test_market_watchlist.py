"""Synthetic resolver and HTTP contract tests for the private watchlist."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from jwt import InvalidTokenError

from pia_api.core.auth import AuthenticatedUser
from pia_api.domain.market_data import (
    InstrumentIdentity,
    InstrumentKind,
    ListingIdentity,
    ProviderMapping,
    ResolutionCandidate,
    ResolutionOutcome,
    ResolutionStatus,
)
from pia_api.main import create_app
from pia_api.services.market_watchlist import TrustedMarketWatchlistGateway

OWNER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_ID = "22222222-2222-4222-8222-222222222222"
INSTRUMENT_ID = UUID("33333333-3333-4333-8333-333333333333")


class SyntheticResolver:
    def __init__(self, outcomes: dict[str, ResolutionOutcome]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    async def resolve_isin(self, isin: str) -> ResolutionOutcome:
        self.calls.append(isin)
        return self.outcomes[isin]


def _outcome(
    status: ResolutionStatus,
    *,
    isin: str = "US0378331005",
    candidate_count: int = 0,
) -> ResolutionOutcome:
    candidates = tuple(
        ResolutionCandidate(
            instrument=InstrumentIdentity(
                isin=isin,
                share_class_figi=f"BBG00000000{index + 1}",
                instrument_kind=InstrumentKind.COMMON_STOCK,
            ),
            display_name=f"Synthetic Equity {index + 1}",
            listing=ListingIdentity(
                instrument_id=UUID(int=INSTRUMENT_ID.int + index),
                mic="XNAS" if index == 0 else "XNYS",
                quote_currency="USD",
            ),
            mapping=ProviderMapping(
                instrument_id=UUID(int=INSTRUMENT_ID.int + index),
                provider="synthetic-eod",
                provider_symbol=f"SYN{index + 1}",
                provider_exchange_code="NAS" if index == 0 else "NYS",
                mic="XNAS" if index == 0 else "XNYS",
                quote_currency="USD",
                mapping_version=1,
                valid_from=datetime(2026, 8, 25, tzinfo=UTC),
                resolved_at=datetime(2026, 8, 25, tzinfo=UTC),
                resolution_source_url="https://resolver.example.test/v3/mapping",
                resolution_status=ResolutionStatus.SUPPORTED,
            ),
        )
        for index in range(candidate_count)
    )
    return ResolutionOutcome(
        requested_isin=isin,
        provider="synthetic-resolver",
        status=status,
        retrieved_at=datetime(2026, 8, 25, 12, tzinfo=UTC),
        source_url="https://resolver.example.test/v3/mapping",
        candidates=candidates,
    )


class MemoryWatchlistGateway(TrustedMarketWatchlistGateway):
    def __init__(self, resolver: SyntheticResolver) -> None:
        self._resolver = resolver
        self.entries: dict[str, dict[str, object]] = {}

    async def _find_entry_by_isin(self, user_id: str, isin: str):
        return self.entries.get(f"{user_id}:{isin}")

    async def _persist_supported(self, user_id: str, outcome: ResolutionOutcome):
        candidate = outcome.candidates[0]
        key = f"{user_id}:{outcome.requested_isin}"
        entry = {
            "id": f"entry-{user_id}",
            "instrument_id": str(candidate.listing.instrument_id),
            "isin": outcome.requested_isin,
            "share_class_figi": candidate.instrument.share_class_figi,
            "instrument_kind": candidate.instrument.instrument_kind.value,
            "display_name": candidate.display_name,
            "mic": candidate.listing.mic,
            "quote_currency": candidate.listing.quote_currency,
            "provider": candidate.mapping.provider,
            "provider_symbol": candidate.mapping.provider_symbol,
            "added_at": "2026-08-25T12:00:00+00:00",
        }
        self.entries[key] = entry
        return entry, False


def test_synthetic_resolver_outcomes_are_distinct_and_invalid_is_local() -> None:
    resolver = SyntheticResolver(
        {
            "US0378331005": _outcome(ResolutionStatus.SUPPORTED, candidate_count=1),
            "US5949181045": _outcome(
                ResolutionStatus.AMBIGUOUS,
                isin="US5949181045",
                candidate_count=2,
            ),
            "US02079K3059": _outcome(ResolutionStatus.UNSUPPORTED, isin="US02079K3059"),
            "US0231351067": _outcome(
                ResolutionStatus.TEMPORARILY_UNAVAILABLE, isin="US0231351067"
            ),
        }
    )
    gateway = MemoryWatchlistGateway(resolver)
    owner = AuthenticatedUser(id=OWNER_ID, email=None)

    invalid = asyncio.run(gateway.add(owner, "US0378331004"))
    unsupported = asyncio.run(gateway.add(owner, "US02079K3059"))
    ambiguous = asyncio.run(gateway.add(owner, "US5949181045"))
    unavailable = asyncio.run(gateway.add(owner, "US0231351067"))
    added = asyncio.run(gateway.add(owner, "US0378331005"))
    duplicate = asyncio.run(gateway.add(owner, "US0378331005"))

    assert [
        result.status
        for result in (invalid, unsupported, ambiguous, unavailable, added, duplicate)
    ] == [
        "invalid",
        "unsupported",
        "ambiguous",
        "temporarily_unavailable",
        "added",
        "duplicate",
    ]
    assert invalid.action == "Correct the ISIN and try again."
    assert len(ambiguous.candidates) == 2
    assert resolver.calls == [
        "US02079K3059",
        "US5949181045",
        "US0231351067",
        "US0378331005",
    ]


class Verifier:
    async def verify(self, token: str) -> AuthenticatedUser:
        if token not in {"owner-token", "other-token"}:
            raise InvalidTokenError("bad token")
        user_id = OWNER_ID if token == "owner-token" else OTHER_ID
        return AuthenticatedUser(id=user_id, email=None)


@dataclass
class ApiGateway:
    removed: list[tuple[str, str]]

    async def list_entries(self, user: AuthenticatedUser):
        return (
            []
            if user.id == OTHER_ID
            else [
                {
                    "id": "entry-owner",
                    "instrument_id": str(INSTRUMENT_ID),
                    "isin": "US0378331005",
                    "share_class_figi": "BBG000000001",
                    "instrument_kind": "common_stock",
                    "display_name": "Synthetic Equity",
                    "mic": "XNAS",
                    "quote_currency": "USD",
                    "provider": "synthetic-eod",
                    "provider_symbol": "SYN1",
                    "added_at": "2026-08-25T12:00:00Z",
                }
            ]
        )

    async def add(self, user: AuthenticatedUser, isin: str):
        assert user.id == OWNER_ID
        return {
            "status": "temporarily_unavailable",
            "action": "Try again later; no permanent unsupported result was stored.",
            "entry": None,
            "candidates": [],
        }

    async def remove(self, user: AuthenticatedUser, entry_id: str):
        if user.id != OWNER_ID or entry_id != "entry-owner":
            return False
        self.removed.append((user.id, entry_id))
        return True

    async def list_portfolio_candidates(self, user: AuthenticatedUser):
        return (
            []
            if user.id == OTHER_ID
            else [
                {
                    "source_instrument_id": "BROKER-SYMBOL",
                    "source_kind": "observed",
                    "quantity": "2.500",
                    "evidence_event_ids": ["event-1"],
                    "snapshot_id": "snapshot-1",
                    "snapshot_as_of": "2026-08-25T10:00:00Z",
                    "snapshot_refreshed_at": "2026-08-25T12:00:00Z",
                    "coverage_status": "unsupported_source_identity",
                    "instrument": None,
                    "action": "Supply a validated ISIN; PIA will not infer one.",
                }
            ]
        )


def _client() -> tuple[TestClient, ApiGateway]:
    app = create_app()
    app.state.jwt_verifier = Verifier()
    gateway = ApiGateway([])
    app.state.market_watchlist_gateway = gateway
    return TestClient(app), gateway


def test_watchlist_routes_require_authentication_and_keep_outcomes_actionable() -> None:
    client, _ = _client()
    headers = {"Authorization": "Bearer owner-token"}

    assert client.get("/v1/market/watchlist").status_code == 401
    assert (
        client.post("/v1/market/watchlist", json={"isin": "US0378331005"}).status_code
        == 401
    )
    assert client.delete("/v1/market/watchlist/entry-owner").status_code == 401

    listed = client.get("/v1/market/watchlist", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["isin"] == "US0378331005"

    unavailable = client.post(
        "/v1/market/watchlist", headers=headers, json={"isin": "US0231351067"}
    )
    assert unavailable.status_code == 200
    assert unavailable.json()["status"] == "temporarily_unavailable"
    assert "permanent" in unavailable.json()["action"]

    candidates = client.get("/v1/market/portfolio-candidates", headers=headers)
    assert candidates.status_code == 200
    assert candidates.json()[0]["source_kind"] == "observed"
    assert candidates.json()[0]["coverage_status"] == "unsupported_source_identity"


def test_watchlist_remove_and_reads_are_owner_scoped() -> None:
    client, gateway = _client()

    other_headers = {"Authorization": "Bearer other-token"}
    assert client.get("/v1/market/watchlist", headers=other_headers).json() == []
    assert (
        client.get("/v1/market/portfolio-candidates", headers=other_headers).json()
        == []
    )
    assert (
        client.delete(
            "/v1/market/watchlist/entry-owner", headers=other_headers
        ).status_code
        == 404
    )

    assert (
        client.delete(
            "/v1/market/watchlist/entry-owner",
            headers={"Authorization": "Bearer owner-token"},
        ).status_code
        == 204
    )
    assert gateway.removed == [(OWNER_ID, "entry-owner")]


def test_watchlist_remove_is_allowed_by_browser_cors_policy() -> None:
    client, _ = _client()

    response = client.options(
        "/v1/market/watchlist/entry-owner",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert "DELETE" in response.headers["access-control-allow-methods"]
