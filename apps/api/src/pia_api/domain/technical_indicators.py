"""Pure, deterministic daily SMA and Wilder RSI analysis.

Bars are ordered by market date, calendar gaps are never filled, and only the
highest correction revision contributes to a result. Indicator values are
analysis evidence, not advice, predictions, or promised returns.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from uuid import UUID

from pia_api.domain.market_data import (
    CompletenessStatus,
    DailyBar,
    FreshnessStatus,
    MarketDataContract,
)

_OUTPUT_QUANTUM = Decimal("0.000000000001")


class IndicatorCode(StrEnum):
    SMA_20 = "sma_20"
    SMA_50 = "sma_50"
    SMA_200 = "sma_200"
    RSI_14 = "rsi_14"


class IndicatorStatus(StrEnum):
    AVAILABLE = "available"
    INSUFFICIENT_HISTORY = "insufficient_history"


class IndicatorDiagnosticCode(StrEnum):
    NO_BARS = "INDICATOR_NO_BARS"
    IDENTITY_MISMATCH = "INDICATOR_IDENTITY_MISMATCH"
    CONTRADICTORY_DUPLICATE = "INDICATOR_CONTRADICTORY_DUPLICATE"
    FUTURE_BAR = "INDICATOR_FUTURE_BAR"
    CALENDAR_GAP = "INDICATOR_CALENDAR_GAP"
    SOURCE_INCOMPLETE = "INDICATOR_SOURCE_INCOMPLETE"
    SOURCE_PENDING = "INDICATOR_SOURCE_PENDING"
    SOURCE_STALE = "INDICATOR_SOURCE_STALE"
    CORRECTED_INPUT = "INDICATOR_CORRECTED_INPUT"


class IndicatorDiagnostic(MarketDataContract):
    code: IndicatorDiagnosticCode
    market_date: date | None = None


class TechnicalIndicatorResult(MarketDataContract):
    """One chart-ready daily value with its complete source-quality context."""

    code: IndicatorCode
    market_date: date
    value: Decimal | None
    status: IndicatorStatus
    observation_count: int
    required_observations: int
    window_start: date
    window_end: date
    listing_id: UUID
    provider: str
    provider_symbol: str
    mic: str
    quote_currency: str
    mapping_version: int
    provider_as_of: datetime
    retrieved_at: datetime
    source_urls: tuple[str, ...]
    freshness_status: FreshnessStatus
    completeness_status: CompletenessStatus
    corrected: bool
    diagnostics: tuple[IndicatorDiagnostic, ...] = ()


class TechnicalAnalysis(MarketDataContract):
    """A deterministic series or an explicit fail-closed unavailable outcome."""

    results: tuple[TechnicalIndicatorResult, ...]
    freshness_status: FreshnessStatus
    completeness_status: CompletenessStatus
    diagnostics: tuple[IndicatorDiagnostic, ...] = ()


_PERIODS = {
    IndicatorCode.SMA_20: 20,
    IndicatorCode.SMA_50: 50,
    IndicatorCode.SMA_200: 200,
    IndicatorCode.RSI_14: 14,
}
_DIAGNOSTIC_ORDER = {
    code: position for position, code in enumerate(IndicatorDiagnosticCode)
}


def calculate_technical_indicators(
    bars: Iterable[DailyBar], *, target_date: date
) -> TechnicalAnalysis:
    """Calculate daily SMA-20/50/200 and Wilder RSI-14 from EOD closes.

    RSI-14 warms up after fourteen price changes (fifteen closes). SMA windows
    use observed sessions, never fabricated weekday or holiday bars. A
    same-revision contradiction or cross-listing history makes the entire
    analysis unavailable rather than selecting an arbitrary fact.
    """
    supplied = tuple(bars)
    if not supplied:
        return _unavailable(IndicatorDiagnosticCode.NO_BARS)

    if not _has_one_series_identity(supplied):
        return _unavailable(IndicatorDiagnosticCode.IDENTITY_MISMATCH)
    if any(bar.market_date > target_date for bar in supplied):
        future_date = min(
            bar.market_date for bar in supplied if bar.market_date > target_date
        )
        return _unavailable(IndicatorDiagnosticCode.FUTURE_BAR, future_date)

    selected, corrected_dates, contradiction = _select_revisions(supplied)
    if contradiction is not None:
        return _unavailable(
            IndicatorDiagnosticCode.CONTRADICTORY_DUPLICATE, contradiction
        )

    freshness = _freshness(selected[-1].market_date, target_date)
    rsi_values = _wilder_rsi(tuple(bar.close for bar in selected), period=14)
    results: list[TechnicalIndicatorResult] = []
    for index, bar in enumerate(selected):
        for code in IndicatorCode:
            results.append(
                _build_result(
                    code,
                    selected,
                    index,
                    rsi_values[index],
                    corrected_dates,
                    freshness,
                )
            )

    completeness = (
        CompletenessStatus.INCOMPLETE
        if any(
            result.completeness_status is not CompletenessStatus.COMPLETE
            for result in results
        )
        else CompletenessStatus.COMPLETE
    )
    return TechnicalAnalysis(
        results=tuple(results),
        freshness_status=freshness,
        completeness_status=completeness,
    )


def serialize_technical_analysis(analysis: TechnicalAnalysis) -> str:
    """Return stable JSON for replay and persistence-boundary comparisons."""
    return analysis.model_dump_json(exclude_none=False)


def _select_revisions(
    bars: tuple[DailyBar, ...],
) -> tuple[tuple[DailyBar, ...], frozenset[date], date | None]:
    by_date: dict[date, list[DailyBar]] = {}
    for bar in bars:
        by_date.setdefault(bar.market_date, []).append(bar)

    selected: list[DailyBar] = []
    corrected_dates: set[date] = set()
    for market_date in sorted(by_date):
        by_revision: dict[int, list[DailyBar]] = {}
        for bar in by_date[market_date]:
            by_revision.setdefault(bar.revision, []).append(bar)
        for observations in by_revision.values():
            if any(bar != observations[0] for bar in observations[1:]):
                return (), frozenset(), market_date
        highest_revision = max(by_revision)
        selected.append(by_revision[highest_revision][0])
        if highest_revision > 1 or len(by_revision) > 1:
            corrected_dates.add(market_date)
    return tuple(selected), frozenset(corrected_dates), None


def _build_result(
    code: IndicatorCode,
    bars: tuple[DailyBar, ...],
    index: int,
    rsi_value: Decimal | None,
    corrected_dates: frozenset[date],
    freshness: FreshnessStatus,
) -> TechnicalIndicatorResult:
    period = _PERIODS[code]
    required = period + 1 if code is IndicatorCode.RSI_14 else period
    if code is IndicatorCode.RSI_14:
        start_index = 0
        value = rsi_value
    else:
        start_index = max(0, index - period + 1)
        window_closes = tuple(bar.close for bar in bars[start_index : index + 1])
        value = _sma(window_closes, period) if len(window_closes) == period else None
    evidence = bars[start_index : index + 1]
    status = (
        IndicatorStatus.AVAILABLE
        if value is not None
        else IndicatorStatus.INSUFFICIENT_HISTORY
    )
    corrected = any(bar.market_date in corrected_dates for bar in evidence)
    diagnostics = _result_diagnostics(evidence, corrected, freshness)
    incomplete_codes = {
        IndicatorDiagnosticCode.CALENDAR_GAP,
        IndicatorDiagnosticCode.SOURCE_INCOMPLETE,
    }
    completeness = (
        CompletenessStatus.INCOMPLETE
        if any(diagnostic.code in incomplete_codes for diagnostic in diagnostics)
        else CompletenessStatus.COMPLETE
    )
    current = bars[index]
    return TechnicalIndicatorResult(
        code=code,
        market_date=current.market_date,
        value=value,
        status=status,
        observation_count=len(evidence),
        required_observations=required,
        window_start=evidence[0].market_date,
        window_end=current.market_date,
        listing_id=current.listing_id,
        provider=current.provider,
        provider_symbol=current.provider_symbol,
        mic=current.mic,
        quote_currency=current.quote_currency,
        mapping_version=current.mapping_version,
        provider_as_of=max(bar.provider_as_of for bar in evidence),
        retrieved_at=max(bar.retrieved_at for bar in evidence),
        source_urls=tuple(sorted({bar.source_url for bar in evidence})),
        freshness_status=freshness,
        completeness_status=completeness,
        corrected=corrected,
        diagnostics=diagnostics,
    )


def _wilder_rsi(
    closes: tuple[Decimal, ...], *, period: int
) -> tuple[Decimal | None, ...]:
    values: list[Decimal | None] = [None] * len(closes)
    if len(closes) <= period:
        return tuple(values)
    changes = tuple(current - previous for previous, current in zip(closes, closes[1:]))
    with localcontext() as context:
        context.prec = 50
        average_gain = sum(
            (max(change, Decimal("0")) for change in changes[:period]), Decimal("0")
        ) / Decimal(period)
        average_loss = sum(
            (max(-change, Decimal("0")) for change in changes[:period]), Decimal("0")
        ) / Decimal(period)
        values[period] = _rsi_value(average_gain, average_loss)
        for index in range(period + 1, len(closes)):
            change = changes[index - 1]
            gain = max(change, Decimal("0"))
            loss = max(-change, Decimal("0"))
            average_gain = (average_gain * (period - 1) + gain) / Decimal(period)
            average_loss = (average_loss * (period - 1) + loss) / Decimal(period)
            values[index] = _rsi_value(average_gain, average_loss)
    return tuple(values)


def _rsi_value(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    if average_gain == 0 and average_loss == 0:
        return _quantize(Decimal("50"))
    if average_loss == 0:
        return _quantize(Decimal("100"))
    if average_gain == 0:
        return _quantize(Decimal("0"))
    relative_strength = average_gain / average_loss
    return _quantize(Decimal("100") - Decimal("100") / (1 + relative_strength))


def _sma(closes: tuple[Decimal, ...], period: int) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return _quantize(sum(closes, Decimal("0")) / Decimal(period))


def _result_diagnostics(
    bars: tuple[DailyBar, ...],
    corrected: bool,
    freshness: FreshnessStatus,
) -> tuple[IndicatorDiagnostic, ...]:
    diagnostics: list[IndicatorDiagnostic] = []
    for previous, current in zip(bars, bars[1:]):
        if _weekday_distance(previous.market_date, current.market_date) > 1:
            diagnostics.append(
                IndicatorDiagnostic(
                    code=IndicatorDiagnosticCode.CALENDAR_GAP,
                    market_date=current.market_date,
                )
            )
    for bar in bars:
        if bar.completeness_status is not CompletenessStatus.COMPLETE:
            diagnostics.append(
                IndicatorDiagnostic(
                    code=IndicatorDiagnosticCode.SOURCE_INCOMPLETE,
                    market_date=bar.market_date,
                )
            )
    if freshness is FreshnessStatus.PENDING:
        diagnostics.append(
            IndicatorDiagnostic(
                code=IndicatorDiagnosticCode.SOURCE_PENDING,
                market_date=bars[-1].market_date,
            )
        )
    elif freshness is FreshnessStatus.STALE:
        diagnostics.append(
            IndicatorDiagnostic(
                code=IndicatorDiagnosticCode.SOURCE_STALE,
                market_date=bars[-1].market_date,
            )
        )
    if corrected:
        correction_date = min(bar.market_date for bar in bars if bar.revision > 1)
        diagnostics.append(
            IndicatorDiagnostic(
                code=IndicatorDiagnosticCode.CORRECTED_INPUT,
                market_date=correction_date,
            )
        )
    return _stable_diagnostics(diagnostics)


def _has_one_series_identity(bars: tuple[DailyBar, ...]) -> bool:
    identities = {
        (
            bar.listing_id,
            bar.provider,
            bar.provider_symbol,
            bar.mic,
            bar.quote_currency,
            bar.mapping_version,
        )
        for bar in bars
    }
    return len(identities) == 1


def _freshness(latest: date, target: date) -> FreshnessStatus:
    age = _weekday_distance(latest, target)
    if age <= 0:
        return FreshnessStatus.FRESH
    if age == 1:
        return FreshnessStatus.PENDING
    return FreshnessStatus.STALE


def _weekday_distance(start: date, end: date) -> int:
    count = 0
    current = start
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            count += 1
    return count


def _quantize(value: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        return value.quantize(_OUTPUT_QUANTUM, rounding=ROUND_HALF_EVEN)


def _stable_diagnostics(
    diagnostics: list[IndicatorDiagnostic],
) -> tuple[IndicatorDiagnostic, ...]:
    unique = {(item.code, item.market_date): item for item in diagnostics}
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (_DIAGNOSTIC_ORDER[item[0]], item[1] or date.min),
        )
    )


def _unavailable(
    code: IndicatorDiagnosticCode, market_date: date | None = None
) -> TechnicalAnalysis:
    return TechnicalAnalysis(
        results=(),
        freshness_status=FreshnessStatus.UNAVAILABLE,
        completeness_status=CompletenessStatus.UNAVAILABLE,
        diagnostics=(IndicatorDiagnostic(code=code, market_date=market_date),),
    )
