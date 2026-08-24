"""Contract tests for provider-neutral, provenance-preserving market data."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from pia_api.domain.market_data import (
    CompletenessStatus,
    DailyBar,
    DiagnosticCode,
    FetchOutcome,
    FreshnessStatus,
    InstrumentIdentity,
    InstrumentKind,
    ListingIdentity,
    ProviderMapping,
    QuotaState,
    ResolutionCandidate,
    ResolutionOutcome,
    ResolutionStatus,
    assess_fetch_outcome,
)

INSTRUMENT_ID = UUID("10000000-0000-0000-0000-000000000001")
INGESTION_RUN_ID = UUID("20000000-0000-0000-0000-000000000001")
RETRIEVED_AT = datetime(2026, 8, 22, 6, tzinfo=UTC)
SOURCE_URL = "https://data.example.test/v1/eod?symbol=SYNX.XMAD"


def _mapping() -> ProviderMapping:
    return ProviderMapping(
        instrument_id=INSTRUMENT_ID,
        provider="synthetic-eod",
        provider_symbol="SYNX.XMAD",
        provider_exchange_code="XMAD",
        mic="XMAD",
        quote_currency="EUR",
        mapping_version=1,
        valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
        resolution_source_url="https://mapping.example.test/v3/mapping",
        resolution_status=ResolutionStatus.SUPPORTED,
    )


def _bar(
    *,
    market_date: date = date(2026, 8, 21),
    open_price: object = "10.10",
    high: object = "11.20",
    low: object = "9.90",
    close: object = "11.00",
    volume: int | None = 1200,
    provider_symbol: str = "SYNX.XMAD",
    quote_currency: str = "EUR",
    revision: int = 1,
    response_sha256: str = "a" * 64,
) -> DailyBar:
    return DailyBar(
        listing_id=INSTRUMENT_ID,
        market_date=market_date,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
        provider="synthetic-eod",
        provider_symbol=provider_symbol,
        mic="XMAD",
        quote_currency=quote_currency,
        provider_as_of=datetime(2026, 8, 21, 23, tzinfo=UTC),
        retrieved_at=RETRIEVED_AT,
        ingestion_run_id=INGESTION_RUN_ID,
        source_url=SOURCE_URL,
        mapping_version=1,
        completeness_status=CompletenessStatus.COMPLETE,
        revision=revision,
        response_sha256=response_sha256,
    )


def _fetch(*bars: DailyBar, completeness=CompletenessStatus.COMPLETE) -> FetchOutcome:
    return FetchOutcome(
        ingestion_run_id=INGESTION_RUN_ID,
        provider="synthetic-eod",
        provider_symbol="SYNX.XMAD",
        mic="XMAD",
        quote_currency="EUR",
        requested_start=date(2026, 8, 1),
        requested_end=date(2026, 8, 21),
        provider_as_of=datetime(2026, 8, 21, 23, tzinfo=UTC),
        started_at=RETRIEVED_AT - timedelta(minutes=1),
        retrieved_at=RETRIEVED_AT,
        source_url=SOURCE_URL,
        request_parameters={"symbol": "SYNX.XMAD"},
        response_sha256="a" * 64,
        completeness_status=completeness,
        quota_state=QuotaState(limit=100, used=2, remaining=98),
        bars=bars,
    )


def test_resolution_contract_preserves_identity_mapping_and_evidence() -> None:
    identity = InstrumentIdentity(
        isin="US0000000002",
        share_class_figi="BBG000000001",
        instrument_kind=InstrumentKind.COMMON_STOCK,
    )
    listing = ListingIdentity(
        instrument_id=INSTRUMENT_ID,
        mic="XMAD",
        quote_currency="EUR",
    )
    outcome = ResolutionOutcome(
        requested_isin=identity.isin,
        provider="synthetic-mapper",
        status=ResolutionStatus.SUPPORTED,
        retrieved_at=RETRIEVED_AT,
        source_url="https://mapping.example.test/v3/mapping",
        candidates=(
            ResolutionCandidate(
                instrument=identity,
                display_name="Synthetic Equity",
                listing=listing,
                mapping=_mapping(),
            ),
        ),
    )

    assert outcome.candidates[0].instrument == identity
    assert outcome.candidates[0].mapping.provider_symbol == "SYNX.XMAD"
    assert outcome.source_url == "https://mapping.example.test/v3/mapping"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_isin", "US0000000003"),
        ("source_url", "https://data.example.test/eod?access_key=secret"),
    ],
)
def test_contracts_reject_invalid_identity_and_secret_bearing_urls(
    field: str, value: str
) -> None:
    values = {
        "requested_isin": "US0000000002",
        "provider": "synthetic-mapper",
        "status": ResolutionStatus.INVALID,
        "retrieved_at": RETRIEVED_AT,
        "source_url": "https://mapping.example.test/v3/mapping",
    }
    values[field] = value

    with pytest.raises(ValidationError):
        ResolutionOutcome(**values)


def test_prices_are_decimal_safe_and_binary_floats_are_rejected() -> None:
    bar = _bar(open_price="10.100000000001")

    assert bar.open == Decimal("10.100000000001")
    with pytest.raises(ValidationError, match="floats are not accepted"):
        _bar(open_price=10.1)
    with pytest.raises(ValidationError, match="no more than 12 decimal places"):
        _bar(open_price="10.1000000000001")


def test_identical_replay_is_deterministic_and_contradiction_fails_closed() -> None:
    identical = assess_fetch_outcome(
        _fetch(_bar(), _bar()), mapping=_mapping(), target_date=date(2026, 8, 21)
    )
    contradictory = assess_fetch_outcome(
        _fetch(_bar(), _bar(close="10.50")),
        mapping=_mapping(),
        target_date=date(2026, 8, 21),
    )

    assert identical.accepted_bars == (_bar(),)
    assert identical.freshness_status is FreshnessStatus.FRESH
    assert [item.code for item in identical.diagnostics] == [
        DiagnosticCode.DUPLICATE_IDENTICAL
    ]
    assert contradictory.accepted_bars == ()
    assert contradictory.freshness_status is FreshnessStatus.UNAVAILABLE
    assert [item.code for item in contradictory.diagnostics] == [
        DiagnosticCode.CONTRADICTORY_DUPLICATE,
        DiagnosticCode.MISSING_DATA,
    ]


def test_invalid_partial_and_stale_data_produce_stable_diagnostics() -> None:
    outcome = assess_fetch_outcome(
        _fetch(
            _bar(
                market_date=date(2026, 8, 19),
                high="9.00",
                low="11.00",
                volume=-1,
            ),
            _bar(market_date=date(2026, 8, 19), quote_currency="USD"),
            completeness=CompletenessStatus.INCOMPLETE,
        ),
        mapping=_mapping(),
        target_date=date(2026, 8, 21),
    )

    assert outcome.accepted_bars == ()
    assert outcome.completeness_status is CompletenessStatus.INCOMPLETE
    assert outcome.freshness_status is FreshnessStatus.UNAVAILABLE
    assert [item.code for item in outcome.diagnostics] == [
        DiagnosticCode.CURRENCY_MISMATCH,
        DiagnosticCode.INVALID_OHLC,
        DiagnosticCode.NEGATIVE_VOLUME,
        DiagnosticCode.MISSING_DATA,
    ]


def test_valid_old_bar_is_retained_but_explicitly_marked_stale() -> None:
    outcome = assess_fetch_outcome(
        _fetch(_bar(market_date=date(2026, 8, 19))),
        mapping=_mapping(),
        target_date=date(2026, 8, 21),
    )

    assert len(outcome.accepted_bars) == 1
    assert outcome.freshness_status is FreshnessStatus.STALE
    assert [item.code for item in outcome.diagnostics] == [DiagnosticCode.STALE_DATA]


def test_contradictory_run_provenance_fails_closed_with_stable_diagnostic() -> None:
    outcome = assess_fetch_outcome(
        _fetch(_bar(response_sha256="b" * 64)),
        mapping=_mapping(),
        target_date=date(2026, 8, 21),
    )

    assert outcome.accepted_bars == ()
    assert [item.code for item in outcome.diagnostics] == [
        DiagnosticCode.PROVENANCE_MISMATCH,
        DiagnosticCode.MISSING_DATA,
    ]
