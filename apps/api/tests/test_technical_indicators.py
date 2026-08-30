"""Synthetic hand-worked tests for deterministic EOD technical indicators."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

from pia_api.domain.market_data import CompletenessStatus, DailyBar, FreshnessStatus
from pia_api.domain.technical_indicators import (
    IndicatorCode,
    IndicatorDiagnosticCode,
    IndicatorStatus,
    calculate_technical_indicators,
    serialize_technical_analysis,
)

LISTING_ID = UUID("10000000-0000-0000-0000-000000000001")
INGESTION_RUN_ID = UUID("20000000-0000-0000-0000-000000000001")
SOURCE_URL = "https://data.example.test/v1/eod?symbol=SYNX.XMAD"


def _weekdays(count: int, *, start: date = date(2025, 9, 1)) -> tuple[date, ...]:
    days: list[date] = []
    candidate = start
    while len(days) < count:
        if candidate.weekday() < 5:
            days.append(candidate)
        candidate += timedelta(days=1)
    return tuple(days)


def _bar(
    market_date: date,
    close: str,
    *,
    revision: int = 1,
    completeness: CompletenessStatus = CompletenessStatus.COMPLETE,
    retrieved_at: datetime = datetime(2026, 6, 15, 6, tzinfo=UTC),
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
        retrieved_at=retrieved_at,
        ingestion_run_id=INGESTION_RUN_ID,
        source_url=SOURCE_URL,
        mapping_version=1,
        completeness_status=completeness,
        revision=revision,
        response_sha256=f"{revision:x}" * 64,
    )


def _result(analysis, code: IndicatorCode, market_date: date):
    return next(
        result
        for result in analysis.results
        if result.code is code and result.market_date == market_date
    )


def test_sma_and_rsi_match_hand_worked_decimal_fixtures() -> None:
    days = _weekdays(200)
    bars = tuple(_bar(day, str(index)) for index, day in enumerate(days, start=1))

    analysis = calculate_technical_indicators(bars, target_date=days[-1])

    assert _result(analysis, IndicatorCode.SMA_20, days[-1]).value == Decimal(
        "190.500000000000"
    )
    assert _result(analysis, IndicatorCode.SMA_50, days[-1]).value == Decimal(
        "175.500000000000"
    )
    assert _result(analysis, IndicatorCode.SMA_200, days[-1]).value == Decimal(
        "100.500000000000"
    )
    assert _result(analysis, IndicatorCode.RSI_14, days[14]).value == Decimal(
        "100.000000000000"
    )

    rsi_closes = (
        "44.34",
        "44.09",
        "44.15",
        "43.61",
        "44.33",
        "44.83",
        "45.10",
        "45.42",
        "45.84",
        "46.08",
        "45.89",
        "46.03",
        "45.61",
        "46.28",
        "46.28",
    )
    rsi_days = _weekdays(len(rsi_closes))
    rsi_analysis = calculate_technical_indicators(
        tuple(
            _bar(day, close) for day, close in zip(rsi_days, rsi_closes, strict=True)
        ),
        target_date=rsi_days[-1],
    )

    assert _result(rsi_analysis, IndicatorCode.RSI_14, rsi_days[-1]).value == Decimal(
        "70.464135021097"
    )


def test_warm_up_is_explicit_for_every_daily_series() -> None:
    days = _weekdays(19)
    analysis = calculate_technical_indicators(
        tuple(_bar(day, str(index)) for index, day in enumerate(days, start=1)),
        target_date=days[-1],
    )

    sma = _result(analysis, IndicatorCode.SMA_20, days[-1])
    rsi = _result(analysis, IndicatorCode.RSI_14, days[13])

    assert sma.status is IndicatorStatus.INSUFFICIENT_HISTORY
    assert sma.value is None
    assert (sma.observation_count, sma.required_observations) == (19, 20)
    assert rsi.status is IndicatorStatus.INSUFFICIENT_HISTORY
    assert rsi.value is None
    assert (rsi.observation_count, rsi.required_observations) == (14, 15)


def test_rsi_zero_gain_and_loss_boundaries_are_explicit() -> None:
    days = _weekdays(15)
    flat = calculate_technical_indicators(
        tuple(_bar(day, "10") for day in days), target_date=days[-1]
    )
    falling = calculate_technical_indicators(
        tuple(_bar(day, str(20 - index)) for index, day in enumerate(days)),
        target_date=days[-1],
    )

    assert _result(flat, IndicatorCode.RSI_14, days[-1]).value == Decimal(
        "50.000000000000"
    )
    assert _result(falling, IndicatorCode.RSI_14, days[-1]).value == Decimal("0E-12")


def test_order_and_identical_duplicates_do_not_change_serialized_results() -> None:
    days = _weekdays(20)
    bars = tuple(_bar(day, str(index)) for index, day in enumerate(days, start=1))
    baseline = calculate_technical_indicators(bars, target_date=days[-1])
    replay = calculate_technical_indicators(
        tuple(reversed(bars)) + (bars[7],), target_date=days[-1]
    )

    assert serialize_technical_analysis(replay) == serialize_technical_analysis(
        baseline
    )


def test_calendar_gap_is_not_filled_and_marks_crossing_windows_incomplete() -> None:
    complete_days = _weekdays(21)
    days = complete_days[:10] + complete_days[11:]
    bars = tuple(_bar(day, str(index)) for index, day in enumerate(days, start=1))

    analysis = calculate_technical_indicators(bars, target_date=days[-1])
    sma = _result(analysis, IndicatorCode.SMA_20, days[-1])

    assert sma.value == Decimal("10.500000000000")
    assert sma.completeness_status is CompletenessStatus.INCOMPLETE
    assert [diagnostic.code for diagnostic in sma.diagnostics] == [
        IndicatorDiagnosticCode.CALENDAR_GAP
    ]


def test_correction_replaces_old_revision_and_marks_affected_outputs() -> None:
    days = _weekdays(21)
    bars = tuple(_bar(day, str(index)) for index, day in enumerate(days, start=1))
    correction = _bar(days[0], "21", revision=2)

    analysis = calculate_technical_indicators(
        bars + (correction,), target_date=days[-1]
    )

    corrected = _result(analysis, IndicatorCode.SMA_20, days[19])
    no_longer_affected = _result(analysis, IndicatorCode.SMA_20, days[20])
    rsi = _result(analysis, IndicatorCode.RSI_14, days[20])
    assert corrected.value == Decimal("11.500000000000")
    assert corrected.corrected is True
    assert corrected.completeness_status is CompletenessStatus.COMPLETE
    assert no_longer_affected.corrected is False
    assert rsi.corrected is True


def test_contradictory_same_revision_duplicate_fails_closed() -> None:
    day = _weekdays(1)[0]
    first = _bar(day, "10")
    contradictory = first.model_copy(update={"close": Decimal("11")})

    analysis = calculate_technical_indicators((first, contradictory), target_date=day)

    assert analysis.results == ()
    assert analysis.completeness_status is CompletenessStatus.UNAVAILABLE
    assert [diagnostic.code for diagnostic in analysis.diagnostics] == [
        IndicatorDiagnosticCode.CONTRADICTORY_DUPLICATE
    ]


def test_stale_and_incomplete_sources_propagate_to_each_result() -> None:
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
    target = days[-1] + timedelta(days=4)

    analysis = calculate_technical_indicators(bars, target_date=target)
    final_results = [
        result for result in analysis.results if result.market_date == days[-1]
    ]

    assert analysis.freshness_status is FreshnessStatus.STALE
    assert final_results
    assert all(
        result.freshness_status is FreshnessStatus.STALE for result in final_results
    )
    assert all(result.provider_as_of is not None for result in final_results)
    assert all(result.retrieved_at is not None for result in final_results)
    assert all(
        IndicatorDiagnosticCode.SOURCE_STALE
        in {diagnostic.code for diagnostic in result.diagnostics}
        for result in final_results
    )
    assert (
        _result(analysis, IndicatorCode.SMA_20, days[-1]).completeness_status
        is CompletenessStatus.INCOMPLETE
    )


def test_empty_history_is_explicitly_unavailable() -> None:
    analysis = calculate_technical_indicators((), target_date=date(2026, 8, 25))

    assert analysis.results == ()
    assert analysis.freshness_status is FreshnessStatus.UNAVAILABLE
    assert analysis.completeness_status is CompletenessStatus.UNAVAILABLE
    assert [diagnostic.code for diagnostic in analysis.diagnostics] == [
        IndicatorDiagnosticCode.NO_BARS
    ]


def test_mixed_identity_and_future_history_fail_closed() -> None:
    days = _weekdays(2)
    first = _bar(days[0], "10")
    other_listing = first.model_copy(
        update={"listing_id": UUID("10000000-0000-0000-0000-000000000002")}
    )
    mixed = calculate_technical_indicators((first, other_listing), target_date=days[0])
    future = calculate_technical_indicators(
        (first, _bar(days[1], "11")), target_date=days[0]
    )

    assert [diagnostic.code for diagnostic in mixed.diagnostics] == [
        IndicatorDiagnosticCode.IDENTITY_MISMATCH
    ]
    assert mixed.results == ()
    assert [diagnostic.code for diagnostic in future.diagnostics] == [
        IndicatorDiagnosticCode.FUTURE_BAR
    ]
    assert future.results == ()


def test_values_are_bounded_and_independent_of_process_decimal_context() -> None:
    days = _weekdays(60)
    closes = tuple(
        Decimal("100") + Decimal((index % 9) - 4) / Decimal("8")
        for index in range(len(days))
    )
    bars = tuple(_bar(day, str(close)) for day, close in zip(days, closes, strict=True))
    baseline = calculate_technical_indicators(bars, target_date=days[-1])

    with localcontext() as context:
        context.prec = 6
        replay = calculate_technical_indicators(bars, target_date=days[-1])

    assert serialize_technical_analysis(replay) == serialize_technical_analysis(
        baseline
    )
    for result in baseline.results:
        if result.value is None:
            continue
        if result.code is IndicatorCode.RSI_14:
            assert Decimal("0") <= result.value <= Decimal("100")
        else:
            evidence = [
                bar.close
                for bar in bars
                if result.window_start <= bar.market_date <= result.window_end
            ]
            assert min(evidence) <= result.value <= max(evidence)
