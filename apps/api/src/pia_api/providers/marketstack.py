"""Server-only Marketstack daily EOD adapter with fail-closed normalization."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from pydantic import ValidationError

from pia_api.domain.market_data import (
    CompletenessStatus,
    DailyBar,
    DiagnosticCode,
    DiagnosticSeverity,
    FetchOutcome,
    FetchStatus,
    MarketDiagnostic,
    ProviderMapping,
    QuotaState,
)

MARKETSTACK_ENDPOINT = "https://api.marketstack.com/v2/eod"


class RequestBudget(Protocol):
    async def reserve(self, attempt: int) -> QuotaState | None: ...


class MarketstackDailyBarProvider:
    """Fetch raw EOD observations without exposing credentials or adjusted data."""

    def __init__(
        self,
        access_key: str,
        budget: RequestBudget,
        *,
        timeout_seconds: int = 15,
        max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.0, 0.25),
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not access_key:
            raise ValueError("Marketstack access key is required")
        if not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        self._access_key = access_key
        self._budget = budget
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=timeout_seconds)
        )

    async def fetch(
        self, mapping: ProviderMapping, start_date: date, end_date: date
    ) -> FetchOutcome:
        if mapping.provider != "marketstack":
            raise ValueError("Marketstack adapter requires a marketstack mapping")
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date")
        run_id = uuid4()
        started_at = self._clock()
        public_parameters = {
            "symbols": mapping.provider_symbol,
            "date_from": start_date.isoformat(),
            "date_to": end_date.isoformat(),
            "limit": "1000",
            "offset": "0",
        }
        source_url = f"{MARKETSTACK_ENDPOINT}?{urlencode(public_parameters)}"
        last_quota = QuotaState(limit=100, used=0, remaining=100)
        last_code = DiagnosticCode.PROVIDER_TIMEOUT
        response_bytes = b""

        async with self._client_factory() as client:
            for attempt in range(1, self._max_attempts + 1):
                quota = await self._budget.reserve(attempt)
                if quota is None:
                    return self._failure(
                        mapping,
                        run_id,
                        start_date,
                        end_date,
                        started_at,
                        source_url,
                        public_parameters,
                        last_quota,
                        DiagnosticCode.QUOTA_EXHAUSTED,
                        FetchStatus.QUOTA_EXHAUSTED,
                        response_bytes,
                    )
                last_quota = quota
                try:
                    response = await client.get(
                        MARKETSTACK_ENDPOINT,
                        params={**public_parameters, "access_key": self._access_key},
                    )
                    response_bytes = response.content
                except httpx.TimeoutException:
                    last_code = DiagnosticCode.PROVIDER_TIMEOUT
                    if attempt < self._max_attempts:
                        await self._sleep(_backoff(attempt, None) + self._jitter())
                        continue
                    break
                if response.status_code == 429:
                    last_code = DiagnosticCode.RATE_LIMITED
                    if attempt < self._max_attempts:
                        await self._sleep(
                            _backoff(attempt, response.headers.get("Retry-After"))
                            + self._jitter()
                        )
                        continue
                    break
                if response.status_code >= 500:
                    last_code = DiagnosticCode.PROVIDER_SERVER_ERROR
                    if attempt < self._max_attempts:
                        await self._sleep(_backoff(attempt, None) + self._jitter())
                        continue
                    break
                if response.status_code >= 400:
                    last_code = DiagnosticCode.PROVIDER_REQUEST_REJECTED
                    break
                return self._normalize(
                    mapping,
                    run_id,
                    start_date,
                    end_date,
                    started_at,
                    source_url,
                    public_parameters,
                    last_quota,
                    response_bytes,
                )

        return self._failure(
            mapping,
            run_id,
            start_date,
            end_date,
            started_at,
            source_url,
            public_parameters,
            last_quota,
            last_code,
            FetchStatus.FAILED,
            response_bytes,
        )

    def _normalize(
        self,
        mapping: ProviderMapping,
        run_id,
        start_date: date,
        end_date: date,
        started_at: datetime,
        source_url: str,
        parameters: dict[str, str],
        quota: QuotaState,
        response_bytes: bytes,
    ) -> FetchOutcome:
        retrieved_at = self._clock()
        response_hash = hashlib.sha256(response_bytes).hexdigest()
        try:
            payload = json.loads(response_bytes, parse_float=Decimal)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("data"), list
            ):
                raise ValueError("response data must be an array")
            pagination = payload.get("pagination")
            if not isinstance(pagination, dict):
                raise ValueError("response pagination must be an object")
            bars = tuple(
                self._normalize_bar(
                    item,
                    mapping,
                    run_id,
                    retrieved_at,
                    source_url,
                    response_hash,
                )
                for item in payload["data"]
            )
            incomplete = pagination.get("total", len(bars)) > len(bars)
        except (KeyError, TypeError, ValueError, ValidationError):
            return self._failure(
                mapping,
                run_id,
                start_date,
                end_date,
                started_at,
                source_url,
                parameters,
                quota,
                DiagnosticCode.MALFORMED_RESPONSE,
                FetchStatus.FAILED,
                response_bytes,
            )
        diagnostics = ()
        completeness = CompletenessStatus.COMPLETE
        status = FetchStatus.COMPLETED
        if incomplete:
            diagnostics = (_diagnostic(DiagnosticCode.INCOMPLETE_RESPONSE),)
            completeness = CompletenessStatus.INCOMPLETE
            status = FetchStatus.PARTIAL
        if not bars:
            completeness = CompletenessStatus.UNAVAILABLE
            status = FetchStatus.FAILED
        provider_as_of = max((bar.provider_as_of for bar in bars), default=retrieved_at)
        return FetchOutcome(
            ingestion_run_id=run_id,
            provider="marketstack",
            provider_symbol=mapping.provider_symbol,
            mic=mapping.mic,
            quote_currency=mapping.quote_currency,
            requested_start=start_date,
            requested_end=end_date,
            provider_as_of=provider_as_of,
            started_at=started_at,
            retrieved_at=retrieved_at,
            source_url=source_url,
            request_parameters=parameters,
            response_sha256=response_hash,
            completeness_status=completeness,
            status=status,
            quota_state=quota,
            bars=bars,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _normalize_bar(
        item: object,
        mapping: ProviderMapping,
        run_id,
        retrieved_at: datetime,
        source_url: str,
        response_hash: str,
    ) -> DailyBar:
        if not isinstance(item, dict):
            raise ValueError("bar must be an object")
        if item.get("symbol") != mapping.provider_symbol:
            raise ValueError("bar symbol does not match mapping")
        if item.get("exchange") != mapping.provider_exchange_code:
            raise ValueError("bar exchange does not match mapping")
        if item.get("currency") not in {None, mapping.quote_currency}:
            raise ValueError("bar currency does not match mapping")
        provider_as_of = datetime.fromisoformat(
            str(item["date"]).replace("Z", "+00:00")
        )
        if provider_as_of.tzinfo is None or provider_as_of.utcoffset() is None:
            raise ValueError("bar timestamp must include a timezone offset")
        volume = item.get("volume")
        if isinstance(volume, Decimal):
            if volume != volume.to_integral_value():
                raise ValueError("volume must be an integer")
            volume = int(volume)
        return DailyBar(
            listing_id=mapping.instrument_id,
            market_date=provider_as_of.date(),
            open=item["open"],
            high=item["high"],
            low=item["low"],
            close=item["close"],
            volume=volume,
            provider="marketstack",
            provider_symbol=mapping.provider_symbol,
            mic=mapping.mic,
            quote_currency=mapping.quote_currency,
            provider_as_of=provider_as_of,
            retrieved_at=retrieved_at,
            ingestion_run_id=run_id,
            source_url=source_url,
            mapping_version=mapping.mapping_version,
            completeness_status=CompletenessStatus.COMPLETE,
            revision=1,
            response_sha256=response_hash,
        )

    def _failure(
        self,
        mapping: ProviderMapping,
        run_id,
        start_date: date,
        end_date: date,
        started_at: datetime,
        source_url: str,
        parameters: dict[str, str],
        quota: QuotaState,
        code: DiagnosticCode,
        status: FetchStatus,
        response_bytes: bytes,
    ) -> FetchOutcome:
        retrieved_at = self._clock()
        return FetchOutcome(
            ingestion_run_id=run_id,
            provider="marketstack",
            provider_symbol=mapping.provider_symbol,
            mic=mapping.mic,
            quote_currency=mapping.quote_currency,
            requested_start=start_date,
            requested_end=end_date,
            provider_as_of=retrieved_at,
            started_at=started_at,
            retrieved_at=retrieved_at,
            source_url=source_url,
            request_parameters=parameters,
            response_sha256=hashlib.sha256(response_bytes).hexdigest(),
            completeness_status=CompletenessStatus.UNAVAILABLE,
            status=status,
            quota_state=quota,
            diagnostics=(_diagnostic(code),),
        )


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), 300.0)
        except ValueError:
            pass
    return float(2 ** (attempt - 1))


def _diagnostic(code: DiagnosticCode) -> MarketDiagnostic:
    return MarketDiagnostic(code=code, severity=DiagnosticSeverity.ERROR)
