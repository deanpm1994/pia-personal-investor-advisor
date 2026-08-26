"""Provider-neutral contracts for provenance-preserving daily market data.

The contracts validate source facts and classify their quality. They do not call
providers, choose listings, calculate indicators, convert currencies, or alter
portfolio accounting.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

ProviderName = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"),
]
MicCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z0-9]{4}$")]
CurrencyCode = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z]{3}$")]
Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Isin = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$"),
]
Figi = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z0-9]{12}$")]

_SECRET_QUERY_PARTS = {
    "access_key",
    "apikey",
    "api_key",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}


def _source_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str)):
        raise ValueError("must be a Decimal or decimal string; floats are not accepted")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise ValueError("must be a valid decimal value") from error
    if not result.is_finite():
        raise ValueError("must be finite")
    if result.adjusted() > 15:
        raise ValueError("must fit the fixed 16-digit integer price precision")
    if result != result.quantize(Decimal("0.000000000001")):
        raise ValueError("must use no more than 12 decimal places")
    return result


def _validate_isin(value: str) -> str:
    expanded = "".join(
        str(ord(character) - 55) if character.isalpha() else character
        for character in value
    )
    total = 0
    for index, character in enumerate(reversed(expanded)):
        digit = int(character)
        if index % 2 == 1:
            digit *= 2
        total += digit // 10 + digit % 10
    if total % 10:
        raise ValueError("ISIN check digit is invalid")
    return value


def validate_isin(value: object) -> str:
    """Validate a raw owner-supplied ISIN without calling a provider."""
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", value) is None
    ):
        raise ValueError("ISIN must use the ISO 6166 format")
    return _validate_isin(value)


def _validate_source_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("source URL must be a credential-free HTTPS URL")
    query_names = {name.lower() for name, _ in parse_qsl(parsed.query)}
    if query_names & _SECRET_QUERY_PARTS:
        raise ValueError("source URL must not contain credentials")
    return value


def _validate_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return value


class MarketDataContract(BaseModel):
    """Immutable contract base that rejects undocumented provider fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class InstrumentKind(StrEnum):
    COMMON_STOCK = "common_stock"
    ETF = "etf"


class ResolutionStatus(StrEnum):
    SUPPORTED = "supported"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"
    PROVIDER_DISABLED = "provider_disabled"


class CompletenessStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"


class FreshnessStatus(StrEnum):
    FRESH = "fresh"
    PENDING = "pending"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class FetchStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    PROVIDER_DISABLED = "provider_disabled"
    LICENSE_REVIEW_REQUIRED = "license_review_required"
    QUOTA_EXHAUSTED = "quota_exhausted"


class DiagnosticSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticCode(StrEnum):
    DUPLICATE_IDENTICAL = "MARKET_BAR_DUPLICATE_IDENTICAL"
    CONTRADICTORY_DUPLICATE = "MARKET_BAR_CONTRADICTORY_DUPLICATE"
    PROVIDER_MISMATCH = "MARKET_BAR_PROVIDER_MISMATCH"
    SYMBOL_MISMATCH = "MARKET_BAR_SYMBOL_MISMATCH"
    MIC_MISMATCH = "MARKET_BAR_MIC_MISMATCH"
    CURRENCY_MISMATCH = "MARKET_BAR_CURRENCY_MISMATCH"
    MAPPING_VERSION_MISMATCH = "MARKET_BAR_MAPPING_VERSION_MISMATCH"
    PROVENANCE_MISMATCH = "MARKET_BAR_PROVENANCE_MISMATCH"
    INVALID_PRICE = "MARKET_BAR_INVALID_PRICE"
    INVALID_OHLC = "MARKET_BAR_INVALID_OHLC"
    NEGATIVE_VOLUME = "MARKET_BAR_NEGATIVE_VOLUME"
    OUTSIDE_REQUESTED_RANGE = "MARKET_BAR_OUTSIDE_REQUESTED_RANGE"
    FUTURE_DATA = "MARKET_BAR_FUTURE_DATA"
    MISSING_DATA = "MARKET_BAR_MISSING_DATA"
    STALE_DATA = "MARKET_BAR_STALE_DATA"
    PROVIDER_TIMEOUT = "MARKET_PROVIDER_TIMEOUT"
    RATE_LIMITED = "MARKET_PROVIDER_RATE_LIMITED"
    PROVIDER_SERVER_ERROR = "MARKET_PROVIDER_SERVER_ERROR"
    PROVIDER_REQUEST_REJECTED = "MARKET_PROVIDER_REQUEST_REJECTED"
    MALFORMED_RESPONSE = "MARKET_PROVIDER_MALFORMED_RESPONSE"
    INCOMPLETE_RESPONSE = "MARKET_PROVIDER_INCOMPLETE_RESPONSE"
    QUOTA_EXHAUSTED = "MARKET_PROVIDER_QUOTA_EXHAUSTED"
    PROVIDER_DISABLED = "MARKET_PROVIDER_DISABLED"
    LICENSE_REVIEW_REQUIRED = "MARKET_PROVIDER_LICENSE_REVIEW_REQUIRED"
    ACTIVE_INSTRUMENT_CAP = "MARKET_ACTIVE_INSTRUMENT_CAP"
    PROVIDER_CONTENT_PURGED = "MARKET_PROVIDER_CONTENT_PURGED"


class InstrumentIdentity(MarketDataContract):
    isin: Isin
    share_class_figi: Figi | None = None
    instrument_kind: InstrumentKind

    @field_validator("isin")
    @classmethod
    def validate_isin(cls, value: str) -> str:
        return validate_isin(value)


class ListingIdentity(MarketDataContract):
    instrument_id: UUID
    mic: MicCode
    quote_currency: CurrencyCode


class ProviderMapping(MarketDataContract):
    instrument_id: UUID
    provider: ProviderName
    provider_symbol: Annotated[str, StringConstraints(strict=True, min_length=1)]
    provider_exchange_code: (
        Annotated[str, StringConstraints(strict=True, min_length=1)] | None
    ) = None
    mic: MicCode
    quote_currency: CurrencyCode
    mapping_version: Annotated[int, Field(strict=True, gt=0)]
    valid_from: datetime
    valid_to: datetime | None = None
    resolved_at: datetime
    resolution_source_url: str
    resolution_status: ResolutionStatus

    @field_validator("provider_symbol", "provider_exchange_code")
    @classmethod
    def reject_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("valid_from", "valid_to", "resolved_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        return _validate_aware(value) if value is not None else None

    @field_validator("resolution_source_url")
    @classmethod
    def validate_resolution_source_url(cls, value: str) -> str:
        return _validate_source_url(value)

    @model_validator(mode="after")
    def validate_validity_interval(self) -> ProviderMapping:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return self


class ResolutionCandidate(MarketDataContract):
    instrument: InstrumentIdentity
    display_name: Annotated[str, StringConstraints(strict=True, min_length=1)]
    listing: ListingIdentity
    mapping: ProviderMapping

    @field_validator("display_name")
    @classmethod
    def reject_blank_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display_name must not be blank")
        return value

    @model_validator(mode="after")
    def require_consistent_listing(self) -> ResolutionCandidate:
        if self.listing.instrument_id != self.mapping.instrument_id:
            raise ValueError("candidate mapping references a different instrument")
        if (
            self.listing.mic != self.mapping.mic
            or self.listing.quote_currency != self.mapping.quote_currency
        ):
            raise ValueError("candidate mapping contradicts its listing identity")
        if (
            self.mapping.resolution_status is not ResolutionStatus.SUPPORTED
            or self.mapping.valid_to is not None
        ):
            raise ValueError(
                "resolution candidate requires an active supported mapping"
            )
        return self


class MarketDiagnostic(MarketDataContract):
    code: DiagnosticCode
    severity: DiagnosticSeverity
    market_date: date | None = None


class ResolutionOutcome(MarketDataContract):
    requested_isin: Isin
    provider: ProviderName
    status: ResolutionStatus
    retrieved_at: datetime
    source_url: str
    candidates: tuple[ResolutionCandidate, ...] = ()
    diagnostics: tuple[MarketDiagnostic, ...] = ()

    @field_validator("requested_isin")
    @classmethod
    def validate_isin(cls, value: str) -> str:
        return validate_isin(value)

    @field_validator("retrieved_at")
    @classmethod
    def validate_retrieved_at(cls, value: datetime) -> datetime:
        return _validate_aware(value)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return _validate_source_url(value)

    @model_validator(mode="after")
    def validate_candidate_count(self) -> ResolutionOutcome:
        if self.status is ResolutionStatus.SUPPORTED and len(self.candidates) != 1:
            raise ValueError("supported resolution requires exactly one candidate")
        if self.status is ResolutionStatus.AMBIGUOUS and len(self.candidates) < 2:
            raise ValueError("ambiguous resolution requires at least two candidates")
        if (
            self.status
            in {
                ResolutionStatus.INVALID,
                ResolutionStatus.UNSUPPORTED,
                ResolutionStatus.PROVIDER_DISABLED,
            }
            and self.candidates
        ):
            raise ValueError("resolution status must not carry candidates")
        return self


class QuotaState(MarketDataContract):
    limit: Annotated[int, Field(strict=True, gt=0)]
    used: Annotated[int, Field(strict=True, ge=0)]
    remaining: Annotated[int, Field(strict=True, ge=0)]
    reset_at: datetime | None = None

    @field_validator("reset_at")
    @classmethod
    def validate_reset_at(cls, value: datetime | None) -> datetime | None:
        return _validate_aware(value) if value is not None else None

    @model_validator(mode="after")
    def validate_accounting(self) -> QuotaState:
        if self.used > self.limit or self.remaining > self.limit:
            raise ValueError("quota values exceed the configured limit")
        if self.used + self.remaining != self.limit:
            raise ValueError("used and remaining quota must equal the limit")
        return self


class DailyBar(MarketDataContract):
    listing_id: UUID
    market_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Annotated[int, Field(strict=True)] | None = None
    provider: ProviderName
    provider_symbol: Annotated[str, StringConstraints(strict=True, min_length=1)]
    mic: MicCode
    quote_currency: CurrencyCode
    provider_as_of: datetime
    retrieved_at: datetime
    ingestion_run_id: UUID
    source_url: str
    mapping_version: Annotated[int, Field(strict=True, gt=0)]
    completeness_status: CompletenessStatus
    revision: Annotated[int, Field(strict=True, gt=0)]
    response_sha256: Sha256

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def validate_decimal(cls, value: object) -> Decimal:
        return _source_decimal(value)

    @field_validator("provider_as_of", "retrieved_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _validate_aware(value)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return _validate_source_url(value)


class FetchOutcome(MarketDataContract):
    ingestion_run_id: UUID
    provider: ProviderName
    provider_symbol: Annotated[str, StringConstraints(strict=True, min_length=1)]
    mic: MicCode
    quote_currency: CurrencyCode
    requested_start: date
    requested_end: date
    provider_as_of: datetime
    started_at: datetime
    retrieved_at: datetime
    source_url: str
    request_parameters: dict[str, str]
    response_sha256: Sha256
    completeness_status: CompletenessStatus
    status: FetchStatus = FetchStatus.COMPLETED
    quota_state: QuotaState
    bars: tuple[DailyBar, ...] = ()
    diagnostics: tuple[MarketDiagnostic, ...] = ()

    @field_validator("provider_as_of", "started_at", "retrieved_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _validate_aware(value)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        return _validate_source_url(value)

    @model_validator(mode="after")
    def validate_requested_interval(self) -> FetchOutcome:
        if self.requested_end < self.requested_start:
            raise ValueError("requested_end must not precede requested_start")
        if self.retrieved_at < self.started_at:
            raise ValueError("retrieved_at must not precede started_at")
        if any(bar.ingestion_run_id != self.ingestion_run_id for bar in self.bars):
            raise ValueError("every bar must reference the fetch ingestion run")
        return self


class FetchAssessment(MarketDataContract):
    accepted_bars: tuple[DailyBar, ...]
    freshness_status: FreshnessStatus
    completeness_status: CompletenessStatus
    diagnostics: tuple[MarketDiagnostic, ...]


_DIAGNOSTIC_ORDER = {code: index for index, code in enumerate(DiagnosticCode)}


def assess_fetch_outcome(
    outcome: FetchOutcome,
    *,
    mapping: ProviderMapping,
    target_date: date,
) -> FetchAssessment:
    """Fail closed while retaining deterministic diagnostics for every omission."""
    diagnostics = list(outcome.diagnostics)
    matching_bars: list[DailyBar] = []
    for bar in outcome.bars:
        mismatches = _identity_diagnostics(bar, outcome, mapping)
        diagnostics.extend(mismatches)
        if not mismatches:
            matching_bars.append(bar)

    accepted: list[DailyBar] = []
    by_date: dict[date, list[DailyBar]] = {}
    for bar in matching_bars:
        by_date.setdefault(bar.market_date, []).append(bar)
    for market_date in sorted(by_date):
        observations = by_date[market_date]
        unique = {_bar_fingerprint(bar): bar for bar in observations}
        if len(unique) > 1:
            diagnostics.append(
                _diagnostic(DiagnosticCode.CONTRADICTORY_DUPLICATE, market_date)
            )
            continue
        bar = next(iter(unique.values()))
        if len(observations) > 1:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCode.DUPLICATE_IDENTICAL,
                    market_date,
                    DiagnosticSeverity.WARNING,
                )
            )
        invalid = _bar_diagnostics(bar, outcome, target_date)
        diagnostics.extend(invalid)
        if not invalid:
            accepted.append(bar)

    accepted.sort(key=lambda bar: (bar.market_date, bar.revision))
    freshness = _freshness(tuple(accepted), target_date)
    if not accepted:
        diagnostics.append(_diagnostic(DiagnosticCode.MISSING_DATA))
    elif freshness is FreshnessStatus.STALE:
        diagnostics.append(
            _diagnostic(DiagnosticCode.STALE_DATA, accepted[-1].market_date)
        )
    if outcome.completeness_status is CompletenessStatus.INCOMPLETE and accepted:
        diagnostics.append(_diagnostic(DiagnosticCode.MISSING_DATA))

    completeness = outcome.completeness_status
    if any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics):
        completeness = CompletenessStatus.INCOMPLETE
    return FetchAssessment(
        accepted_bars=tuple(accepted),
        freshness_status=freshness,
        completeness_status=completeness,
        diagnostics=_stable_diagnostics(diagnostics),
    )


def _identity_diagnostics(
    bar: DailyBar, outcome: FetchOutcome, mapping: ProviderMapping
) -> list[MarketDiagnostic]:
    checks = (
        (
            bar.provider != outcome.provider or bar.provider != mapping.provider,
            DiagnosticCode.PROVIDER_MISMATCH,
        ),
        (
            bar.provider_symbol != outcome.provider_symbol
            or bar.provider_symbol != mapping.provider_symbol,
            DiagnosticCode.SYMBOL_MISMATCH,
        ),
        (bar.mic != outcome.mic or bar.mic != mapping.mic, DiagnosticCode.MIC_MISMATCH),
        (
            bar.quote_currency != outcome.quote_currency
            or bar.quote_currency != mapping.quote_currency,
            DiagnosticCode.CURRENCY_MISMATCH,
        ),
        (
            bar.mapping_version != mapping.mapping_version,
            DiagnosticCode.MAPPING_VERSION_MISMATCH,
        ),
        (
            bar.source_url != outcome.source_url
            or bar.retrieved_at != outcome.retrieved_at
            or bar.response_sha256 != outcome.response_sha256,
            DiagnosticCode.PROVENANCE_MISMATCH,
        ),
    )
    return [_diagnostic(code, bar.market_date) for failed, code in checks if failed]


def _bar_diagnostics(
    bar: DailyBar, outcome: FetchOutcome, target_date: date
) -> list[MarketDiagnostic]:
    diagnostics: list[MarketDiagnostic] = []
    prices = (bar.open, bar.high, bar.low, bar.close)
    if any(price <= 0 for price in prices):
        diagnostics.append(_diagnostic(DiagnosticCode.INVALID_PRICE, bar.market_date))
    if (
        bar.high < bar.low
        or bar.high < max(bar.open, bar.close)
        or bar.low > min(bar.open, bar.close)
    ):
        diagnostics.append(_diagnostic(DiagnosticCode.INVALID_OHLC, bar.market_date))
    if bar.volume is not None and bar.volume < 0:
        diagnostics.append(_diagnostic(DiagnosticCode.NEGATIVE_VOLUME, bar.market_date))
    if not outcome.requested_start <= bar.market_date <= outcome.requested_end:
        diagnostics.append(
            _diagnostic(DiagnosticCode.OUTSIDE_REQUESTED_RANGE, bar.market_date)
        )
    if bar.market_date > target_date:
        diagnostics.append(_diagnostic(DiagnosticCode.FUTURE_DATA, bar.market_date))
    return diagnostics


def _bar_fingerprint(bar: DailyBar) -> str:
    return bar.model_dump_json(exclude_none=False)


def _freshness(bars: tuple[DailyBar, ...], target_date: date) -> FreshnessStatus:
    if not bars:
        return FreshnessStatus.UNAVAILABLE
    latest = bars[-1].market_date
    weekday_age = _weekday_distance(latest, target_date)
    if weekday_age <= 0:
        return FreshnessStatus.FRESH
    if weekday_age == 1:
        return FreshnessStatus.PENDING
    return FreshnessStatus.STALE


def _weekday_distance(start: date, end: date) -> int:
    if start >= end:
        return 0
    count = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            count += 1
    return count


def _diagnostic(
    code: DiagnosticCode,
    market_date: date | None = None,
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
) -> MarketDiagnostic:
    return MarketDiagnostic(code=code, severity=severity, market_date=market_date)


def _stable_diagnostics(
    diagnostics: list[MarketDiagnostic],
) -> tuple[MarketDiagnostic, ...]:
    unique = {
        (item.code, item.severity, item.market_date): item for item in diagnostics
    }
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda key: (
                _DIAGNOSTIC_ORDER[key[0]],
                key[2] or date.min,
                key[1],
            ),
        )
    )
