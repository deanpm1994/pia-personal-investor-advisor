"""Financial and HTTP contracts for authenticated market analysis."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from jwt import InvalidTokenError

from pia_api.core.auth import AuthenticatedUser
from pia_api.domain.market_analysis import (
    PositionLot,
    ValuationStatus,
    calculate_native_valuation,
)
from pia_api.domain.market_data import CompletenessStatus, DailyBar
from pia_api.main import create_app
from pia_api.services.market_analysis import (
    AnalysisInstrument,
    AnalysisPosition,
    ProviderAccessStatus,
    _provider_access,
    _target_market_date,
    build_analysis_item,
)

LISTING_ID = UUID("10000000-0000-0000-0000-000000000001")
RUN_ID = UUID("20000000-0000-0000-0000-000000000001")


def _weekdays(count: int, *, start: date = date(2026, 7, 1)) -> tuple[date, ...]:
    result = []
    candidate = start
    while len(result) < count:
        if candidate.weekday() < 5:
            result.append(candidate)
        candidate += timedelta(days=1)
    return tuple(result)


def _bar(
    market_date: date,
    close: str,
    *,
    completeness: CompletenessStatus = CompletenessStatus.COMPLETE,
) -> DailyBar:
    value = Decimal(close)
    return DailyBar(
        listing_id=LISTING_ID,
        market_date=market_date,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=1000,
        provider="synthetic-eod",
        provider_symbol="SYNX.XMAD",
        mic="XMAD",
        quote_currency="EUR",
        provider_as_of=datetime.combine(market_date, datetime.min.time(), UTC),
        retrieved_at=datetime(2026, 8, 29, 6, tzinfo=UTC),
        ingestion_run_id=RUN_ID,
        source_url="https://data.example.test/v1/eod?symbol=SYNX.XMAD",
        mapping_version=1,
        completeness_status=completeness,
        revision=1,
        response_sha256="a" * 64,
    )


def _instrument() -> AnalysisInstrument:
    return AnalysisInstrument(
        instrument_id=LISTING_ID,
        isin="US0000000002",
        share_class_figi="BBG000000001",
        instrument_kind="common_stock",
        display_name="Synthetic Equity",
        mic="XMAD",
        quote_currency="EUR",
        provider="synthetic-eod",
        provider_symbol="SYNX.XMAD",
        mapping_version=1,
        watched=True,
    )


class Verifier:
    async def verify(self, token: str) -> AuthenticatedUser:
        if token not in {"owner-token", "other-token"}:
            raise InvalidTokenError("bad token")
        owner_id = "owner" if token == "owner-token" else "other"
        return AuthenticatedUser(id=owner_id, email=None)


def _has_float(value: object) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_has_float(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_float(item) for item in value)
    return False


def test_native_valuation_reconciles_exact_decimal_basis_and_performance() -> None:
    valuation = calculate_native_valuation(
        position_quantity="2.000000000000",
        current_price="15.000000000000",
        quote_currency="EUR",
        lots=(
            PositionLot(
                quantity="1.250000000000",
                total_basis="12.500000000000",
                source_currency="EUR",
                evidence_event_ids=("buy-1",),
            ),
            PositionLot(
                quantity="0.750000000000",
                total_basis="7.500000000000",
                source_currency="EUR",
                evidence_event_ids=("buy-2", "fee-2"),
            ),
        ),
    )

    assert valuation.status is ValuationStatus.AVAILABLE
    assert valuation.current_value == Decimal("30.000000000000")
    assert valuation.total_basis == Decimal("20.000000000000")
    assert valuation.unrealized_gain == Decimal("10.000000000000")
    assert valuation.unrealized_return_percent == Decimal("50.000000000000")
    assert valuation.evidence_event_ids == ("buy-1", "buy-2", "fee-2")


@pytest.mark.parametrize(
    ("lots", "expected"),
    [
        ((), ValuationStatus.BASIS_UNAVAILABLE),
        (
            (
                PositionLot(
                    quantity="2",
                    total_basis="20",
                    source_currency="USD",
                ),
            ),
            ValuationStatus.CURRENCY_MISMATCH,
        ),
        (
            (
                PositionLot(
                    quantity="1",
                    total_basis="10",
                    source_currency="EUR",
                ),
            ),
            ValuationStatus.QUANTITY_MISMATCH,
        ),
    ],
)
def test_native_valuation_suppresses_unknown_cross_currency_or_partial_results(
    lots: tuple[PositionLot, ...], expected: ValuationStatus
) -> None:
    valuation = calculate_native_valuation(
        position_quantity="2",
        current_price="15",
        quote_currency="EUR",
        lots=lots,
    )

    assert valuation.status is expected
    assert valuation.current_value is None
    assert valuation.total_basis is None
    assert valuation.unrealized_gain is None
    assert valuation.unrealized_return_percent is None


def test_native_valuation_rejects_binary_float_inputs() -> None:
    with pytest.raises(ValueError, match="floats are not accepted"):
        calculate_native_valuation(
            position_quantity=2.0,
            current_price="15",
            quote_currency="EUR",
            lots=(),
        )


def test_analysis_builder_reconciles_bars_indicators_and_native_valuation() -> None:
    days = _weekdays(20)
    bars = tuple(_bar(day, str(index)) for index, day in enumerate(days, start=1))
    position = AnalysisPosition(
        quantity="2",
        evidence_event_ids=("buy-1",),
        lots=(
            PositionLot(
                quantity="2",
                total_basis="20",
                source_currency="EUR",
                evidence_event_ids=("buy-1",),
            ),
        ),
        snapshot_id="snapshot-1",
        snapshot_as_of=datetime(2026, 8, 28, 12, tzinfo=UTC),
        snapshot_refreshed_at=datetime(2026, 8, 28, 13, tzinfo=UTC),
        snapshot_input_fingerprint="b" * 64,
    )

    item = build_analysis_item(
        _instrument(),
        position=position,
        access_status=ProviderAccessStatus.ENABLED,
        bars=bars,
        target_date=days[-1],
    )

    assert item["source_kind"] == "portfolio_and_watchlist"
    assert item["state"] == "ready"
    assert len(item["bars"]) == 20
    assert len(item["indicators"]) == 80
    assert item["indicators"][-4]["value"] == "10.500000000000"
    assert item["valuation"] == {
        "status": "available",
        "quote_currency": "EUR",
        "current_price": "20.000000000000",
        "current_value": "40.000000000000",
        "total_basis": "20.000000000000",
        "unrealized_gain": "20.000000000000",
        "unrealized_return_percent": "100.000000000000",
        "evidence_event_ids": ["buy-1"],
    }


def test_analysis_builder_withholds_content_when_provider_access_is_denied() -> None:
    item = build_analysis_item(
        _instrument(),
        position=None,
        access_status=ProviderAccessStatus.LICENSE_REVIEW_REQUIRED,
        bars=(_bar(_weekdays(1)[0], "10"),),
        target_date=_weekdays(1)[0],
    )

    assert item["state"] == "license_review_required"
    assert item["bars"] == []
    assert item["indicators"] == []
    assert item["source"] is None
    assert item["valuation"] is None
    assert item["diagnostics"] == [
        {"code": "MARKET_LICENSE_REVIEW_REQUIRED", "evidence_event_ids": []}
    ]


def test_analysis_builder_propagates_incomplete_and_stale_history() -> None:
    days = _weekdays(20)
    bars = tuple(
        _bar(
            day,
            str(index),
            completeness=(
                CompletenessStatus.INCOMPLETE
                if index == 10
                else CompletenessStatus.COMPLETE
            ),
        )
        for index, day in enumerate(days, start=1)
    )

    incomplete = build_analysis_item(
        _instrument(),
        position=None,
        access_status=ProviderAccessStatus.ENABLED,
        bars=bars,
        target_date=days[-1],
    )
    stale = build_analysis_item(
        _instrument(),
        position=None,
        access_status=ProviderAccessStatus.ENABLED,
        bars=tuple(_bar(day, str(index)) for index, day in enumerate(days, start=1)),
        target_date=days[-1] + timedelta(days=4),
    )

    assert incomplete["state"] == "incomplete"
    assert incomplete["completeness"] == {"status": "incomplete"}
    assert stale["state"] == "stale"
    assert stale["freshness"] == {"status": "stale"}


def test_schedule_target_and_marketstack_access_fail_closed() -> None:
    assert _target_market_date(date(2026, 8, 31)) == date(2026, 8, 28)
    assert _target_market_date(date(2026, 9, 1)) == date(2026, 8, 31)
    assert _target_market_date(date(2026, 8, 30)) == date(2026, 8, 28)

    now = datetime(2026, 8, 30, tzinfo=UTC)
    base = {
        "provider": "marketstack",
        "access_status": "enabled",
        "license_review_due_at": now + timedelta(days=1),
        "risk_attestation_version": None,
        "risk_attested_at": None,
        "risk_withdrawn_at": None,
    }
    assert _provider_access(base, now) is ProviderAccessStatus.PROVIDER_DISABLED
    accepted = {
        **base,
        "risk_attestation_version": "adr-0009-founder-risk-v1",
        "risk_attested_at": now - timedelta(days=1),
    }
    assert _provider_access(accepted, now) is ProviderAccessStatus.ENABLED
    expired = {**accepted, "license_review_due_at": now}
    assert (
        _provider_access(expired, now) is ProviderAccessStatus.LICENSE_REVIEW_REQUIRED
    )


def _ready_item() -> dict[str, object]:
    return {
        "source_kind": "portfolio_and_watchlist",
        "source_instrument_id": "US0000000002",
        "state": "ready",
        "instrument": {
            "instrument_id": "10000000-0000-0000-0000-000000000001",
            "isin": "US0000000002",
            "share_class_figi": "BBG000000001",
            "instrument_kind": "common_stock",
            "display_name": "Synthetic Equity",
            "mic": "XMAD",
            "quote_currency": "EUR",
            "provider": "synthetic-eod",
            "provider_symbol": "SYNX.XMAD",
        },
        "bars": [
            {
                "market_date": "2026-08-28",
                "open": "14.000000000000",
                "high": "16.000000000000",
                "low": "13.500000000000",
                "close": "15.000000000000",
                "volume": 1000,
                "revision": 1,
                "provider_as_of": "2026-08-28T23:00:00Z",
                "retrieved_at": "2026-08-29T06:00:00Z",
                "source_url": ("https://data.example.test/v1/eod?symbol=SYNX.XMAD"),
                "completeness_status": "complete",
                "corrected": False,
            }
        ],
        "indicators": [
            {
                "code": "sma_20",
                "market_date": "2026-08-28",
                "value": "14.500000000000",
                "status": "available",
                "observation_count": 20,
                "required_observations": 20,
                "window_start": "2026-08-03",
                "window_end": "2026-08-28",
                "provider_as_of": "2026-08-28T23:00:00Z",
                "retrieved_at": "2026-08-29T06:00:00Z",
                "source_urls": ["https://data.example.test/v1/eod?symbol=SYNX.XMAD"],
                "freshness_status": "fresh",
                "completeness_status": "complete",
                "corrected": False,
                "diagnostics": [],
            }
        ],
        "source": {
            "provider": "synthetic-eod",
            "provider_symbol": "SYNX.XMAD",
            "mic": "XMAD",
            "quote_currency": "EUR",
            "attribution": "Market data: synthetic-eod",
            "source_urls": ["https://data.example.test/v1/eod?symbol=SYNX.XMAD"],
            "provider_as_of": "2026-08-28T23:00:00Z",
            "retrieved_at": "2026-08-29T06:00:00Z",
        },
        "freshness": {"status": "fresh"},
        "completeness": {"status": "complete"},
        "position": {
            "quantity": "2.000000000000",
            "evidence_event_ids": ["buy-1", "buy-2"],
            "snapshot_id": "snapshot-1",
            "snapshot_as_of": "2026-08-28T12:00:00Z",
            "snapshot_refreshed_at": "2026-08-28T13:00:00Z",
            "snapshot_input_fingerprint": "b" * 64,
        },
        "valuation": {
            "status": "available",
            "quote_currency": "EUR",
            "current_price": "15.000000000000",
            "current_value": "30.000000000000",
            "total_basis": "20.000000000000",
            "unrealized_gain": "10.000000000000",
            "unrealized_return_percent": "50.000000000000",
            "evidence_event_ids": ["buy-1", "buy-2"],
        },
        "diagnostics": [],
    }


@dataclass
class AnalysisGateway:
    records: dict[str, list[dict[str, object]]]
    calls: list[str] = field(default_factory=list)

    async def list_analysis(self, user: AuthenticatedUser):
        self.calls.append(user.id)
        return self.records.get(user.id, [])


def _client(
    records: dict[str, list[dict[str, object]]],
) -> tuple[TestClient, AnalysisGateway]:
    app = create_app()
    app.state.jwt_verifier = Verifier()
    gateway = AnalysisGateway(records)
    app.state.market_analysis_gateway = gateway
    return TestClient(app), gateway


def test_market_analysis_api_is_authenticated_owner_scoped_and_decimal_safe() -> None:
    client, gateway = _client({"owner": [_ready_item()], "other": []})

    assert client.get("/v1/market/analysis").status_code == 401
    owner = client.get(
        "/v1/market/analysis", headers={"Authorization": "Bearer owner-token"}
    )
    other = client.get(
        "/v1/market/analysis", headers={"Authorization": "Bearer other-token"}
    )

    assert owner.status_code == 200
    assert owner.json()["state"] == "ready"
    assert owner.json()["items"][0]["valuation"]["current_value"] == ("30.000000000000")
    assert owner.json()["items"][0]["source"]["attribution"] == (
        "Market data: synthetic-eod"
    )
    assert not _has_float(owner.json())
    assert other.json() == {"state": "empty", "items": []}
    assert gateway.calls == ["owner", "other"]


def test_market_analysis_api_preserves_distinct_unavailable_states() -> None:
    unavailable_states = (
        "unsupported",
        "provider_disabled",
        "license_review_required",
        "unavailable",
        "stale",
        "incomplete",
    )
    records = []
    for state in unavailable_states:
        item = _ready_item()
        item["state"] = state
        if state in {
            "unsupported",
            "provider_disabled",
            "license_review_required",
            "unavailable",
        }:
            item["bars"] = []
            item["indicators"] = []
            item["source"] = None
            item["valuation"] = None
        records.append(item)
    client, _ = _client({"owner": records})

    response = client.get(
        "/v1/market/analysis", headers={"Authorization": "Bearer owner-token"}
    )

    assert response.status_code == 200
    assert [item["state"] for item in response.json()["items"]] == list(
        unavailable_states
    )


def test_market_analysis_gateway_absence_is_actionable() -> None:
    app = create_app()
    app.state.jwt_verifier = Verifier()
    app.state.market_analysis_gateway = None
    client = TestClient(app)

    response = client.get(
        "/v1/market/analysis", headers={"Authorization": "Bearer owner-token"}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MARKET_ANALYSIS_UNAVAILABLE"
