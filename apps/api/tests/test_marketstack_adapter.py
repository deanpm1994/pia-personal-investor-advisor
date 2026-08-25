"""Synthetic contract tests for the server-only Marketstack adapter."""

import asyncio
import json
from datetime import UTC, date, datetime
from uuid import UUID

import httpx

from pia_api.domain.market_data import (
    DiagnosticCode,
    FetchStatus,
    ProviderMapping,
    QuotaState,
    ResolutionStatus,
)
from pia_api.providers.marketstack import MarketstackDailyBarProvider


class _Budget:
    def __init__(self, available: int = 100) -> None:
        self.available = available
        self.attempts: list[int] = []

    async def reserve(self, attempt: int) -> QuotaState | None:
        self.attempts.append(attempt)
        if len(self.attempts) > self.available:
            return None
        return QuotaState(
            limit=100,
            used=len(self.attempts),
            remaining=100 - len(self.attempts),
        )


def _mapping() -> ProviderMapping:
    return ProviderMapping(
        instrument_id=UUID("10000000-0000-0000-0000-000000000001"),
        provider="marketstack",
        provider_symbol="SYNX",
        provider_exchange_code="XMAD",
        mic="XMAD",
        quote_currency="EUR",
        mapping_version=1,
        valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 8, 1, tzinfo=UTC),
        resolution_source_url="https://mapping.example.test/marketstack/SYNX",
        resolution_status=ResolutionStatus.SUPPORTED,
    )


def _response(*, close: str = "11.00", total: int = 1) -> bytes:
    return json.dumps(
        {
            "pagination": {"limit": 1000, "offset": 0, "count": 1, "total": total},
            "data": [
                {
                    "open": "10.10",
                    "high": "11.20",
                    "low": "9.90",
                    "close": close,
                    "adj_close": "999.99",
                    "volume": 1200,
                    "symbol": "SYNX",
                    "exchange": "XMAD",
                    "currency": "EUR",
                    "date": "2026-08-25T00:00:00+00:00",
                }
            ],
        }
    ).encode()


def _provider(handler, budget: _Budget, sleeps: list[float] | None = None):
    async def sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    transport = httpx.MockTransport(handler)
    return MarketstackDailyBarProvider(
        "private-key",
        budget,
        sleep=sleep,
        jitter=lambda: 0.125,
        client_factory=lambda: httpx.AsyncClient(transport=transport),
        clock=lambda: datetime(2026, 8, 26, 6, tzinfo=UTC),
    )


def test_adapter_normalizes_raw_ohlcv_without_leaking_the_key() -> None:
    seen_request: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_request.append(request)
        return httpx.Response(200, content=_response())

    outcome = asyncio.run(
        _provider(handler, _Budget()).fetch(
            _mapping(), date(2026, 8, 25), date(2026, 8, 25)
        )
    )

    assert outcome.status is FetchStatus.COMPLETED
    assert str(outcome.bars[0].close) == "11.00"
    assert "999.99" not in str(outcome.bars[0])
    assert seen_request[0].url.params["access_key"] == "private-key"
    assert "access_key" not in outcome.source_url
    assert "access_key" not in outcome.request_parameters


def test_adapter_retries_bounded_transient_failures_and_honors_retry_after() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(503),
            httpx.Response(200, content=_response()),
        ]
    )
    sleeps: list[float] = []
    budget = _Budget()

    outcome = asyncio.run(
        _provider(lambda _: next(responses), budget, sleeps).fetch(
            _mapping(), date(2026, 8, 25), date(2026, 8, 25)
        )
    )

    assert outcome.status is FetchStatus.COMPLETED
    assert budget.attempts == [1, 2, 3]
    assert sleeps == [2.125, 2.125]


def test_adapter_fails_closed_for_malformed_or_incomplete_responses() -> None:
    malformed = asyncio.run(
        _provider(
            lambda _: httpx.Response(200, json={"data": [{"date": "bad"}]}),
            _Budget(),
        ).fetch(_mapping(), date(2026, 8, 25), date(2026, 8, 25))
    )
    incomplete = asyncio.run(
        _provider(
            lambda _: httpx.Response(200, content=_response(total=2)), _Budget()
        ).fetch(_mapping(), date(2026, 8, 25), date(2026, 8, 25))
    )

    assert malformed.status is FetchStatus.FAILED
    assert malformed.diagnostics[0].code is DiagnosticCode.MALFORMED_RESPONSE
    assert incomplete.status is FetchStatus.PARTIAL
    assert incomplete.diagnostics[0].code is DiagnosticCode.INCOMPLETE_RESPONSE


def test_adapter_does_not_call_provider_after_quota_is_exhausted() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_response())

    outcome = asyncio.run(
        _provider(handler, _Budget(available=0)).fetch(
            _mapping(), date(2026, 8, 25), date(2026, 8, 25)
        )
    )

    assert outcome.status is FetchStatus.QUOTA_EXHAUSTED
    assert outcome.diagnostics[0].code is DiagnosticCode.QUOTA_EXHAUSTED
    assert calls == 0


def test_adapter_records_provider_outage_after_bounded_attempts() -> None:
    budget = _Budget()

    outcome = asyncio.run(
        _provider(lambda _: httpx.Response(503), budget).fetch(
            _mapping(), date(2026, 8, 25), date(2026, 8, 25)
        )
    )

    assert outcome.status is FetchStatus.FAILED
    assert outcome.diagnostics[0].code is DiagnosticCode.PROVIDER_SERVER_ERROR
    assert budget.attempts == [1, 2, 3]
