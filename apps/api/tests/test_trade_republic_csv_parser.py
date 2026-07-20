"""Tests for the deterministic, validation-only Trade Republic CSV adapter."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from pia_api.providers.trade_republic_csv import (
    DIAGNOSTIC_DUPLICATE_SOURCE_IDENTITY,
    DIAGNOSTIC_INVALID_CURRENCY,
    DIAGNOSTIC_INVALID_FX_EVIDENCE,
    DIAGNOSTIC_ROW_SHAPE,
    TradeRepublicCsvStagedEvent,
    parse_trade_republic_csv,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "trade_republic_csv_v1"


def _fixture_text(relative_path: str) -> str:
    return (FIXTURE_ROOT / relative_path).read_text(encoding="utf-8-sig")


def test_parses_supported_observed_rows_into_decimal_backed_staged_candidates() -> None:
    batch = parse_trade_republic_csv(_fixture_text("accepted-observed.csv"))
    manifest = json.loads((FIXTURE_ROOT / "expected-events.json").read_text())

    assert batch.format_version == "trade-republic-csv-v1"
    assert batch.confirmation_eligible is True
    assert batch.diagnostics == ()
    assert [
        (candidate.source_identity.event_reference, candidate.event_type.value)
        for row in batch.rows
        for candidate in row.candidates
    ] == [
        (event["event_reference"], event["event_type"])
        for event in manifest["observed_expected_events"]
    ]

    buy = next(
        candidate
        for row in batch.rows
        for candidate in row.candidates
        if candidate.source_identity.event_reference == "synthetic-tr-buy:base"
    )
    assert isinstance(buy, TradeRepublicCsvStagedEvent)
    assert buy.legs[0].money.amount == Decimal("100.00")
    assert buy.legs[1].quantity.value == Decimal("2.500")

    fx_dividend = next(
        candidate
        for row in batch.rows
        for candidate in row.candidates
        if candidate.source_identity.event_reference == "synthetic-tr-fx-dividend:base"
    )
    assert fx_dividend.legs[0].money.amount == Decimal("10.00")
    assert fx_dividend.legs[0].money.currency == "USD"
    assert fx_dividend.source_reported_eur.eur_amount.amount == Decimal("9.20")
    assert fx_dividend.source_reported_eur.source_rate == Decimal("0.9200")


@pytest.mark.parametrize(
    ("fixture", "expected_code"),
    [
        (item["fixture"], item["diagnostic_code"])
        for item in json.loads((FIXTURE_ROOT / "expected-events.json").read_text())[
            "malformed"
        ]
    ],
)
def test_malformed_fixture_rows_produce_actionable_diagnostics(
    fixture: str, expected_code: str
) -> None:
    batch = parse_trade_republic_csv(_fixture_text(fixture))
    diagnostics = [
        diagnostic for row in batch.rows for diagnostic in row.diagnostics
    ] + list(batch.diagnostics)

    assert batch.confirmation_eligible is False
    assert any(diagnostic.code == expected_code for diagnostic in diagnostics)
    assert all(diagnostic.message for diagnostic in diagnostics)


def test_unsupported_test_only_extension_is_not_accepted_as_a_production_dialect() -> (
    None
):
    batch = parse_trade_republic_csv(_fixture_text("extension-phase3-test-only.csv"))

    assert batch.confirmation_eligible is False
    assert {
        diagnostic.code for row in batch.rows for diagnostic in row.diagnostics
    } == {"TRCSV013_UNSUPPORTED_SOURCE_TYPE"}
    assert all(not row.candidates for row in batch.rows)


def test_duplicate_source_component_blocks_the_entire_batch() -> None:
    lines = _fixture_text("accepted-observed.csv").splitlines()
    duplicate_row = lines[1]
    batch = parse_trade_republic_csv("\n".join([lines[0], lines[1], duplicate_row]))

    assert batch.confirmation_eligible is False
    assert batch.rows[0].candidates
    assert batch.rows[1].candidates == ()
    assert batch.rows[1].diagnostics[0].code == DIAGNOSTIC_DUPLICATE_SOURCE_IDENTITY


def test_malformed_row_width_blocks_confirmation_with_its_own_diagnostic() -> None:
    lines = _fixture_text("accepted-observed.csv").splitlines()
    batch = parse_trade_republic_csv(
        "\n".join([lines[0], ",".join(lines[1].split(",")[:-1])])
    )

    assert batch.confirmation_eligible is False
    assert batch.rows[0].diagnostics[0].code == DIAGNOSTIC_ROW_SHAPE


@pytest.mark.parametrize(
    ("source", "expected_code"),
    [
        (
            _fixture_text("accepted-observed.csv").replace(
                "1000.00,,,EUR", "1000.00,,,eur", 1
            ),
            DIAGNOSTIC_INVALID_CURRENCY,
        ),
        (
            _fixture_text("accepted-observed.csv").replace("0.9200", "0", 1),
            DIAGNOSTIC_INVALID_FX_EVIDENCE,
        ),
    ],
)
def test_invalid_currency_or_fx_evidence_blocks_confirmation(
    source: str, expected_code: str
) -> None:
    batch = parse_trade_republic_csv(source)

    assert batch.confirmation_eligible is False
    assert any(
        diagnostic.code == expected_code
        for row in batch.rows
        for diagnostic in row.diagnostics
    )


def test_parser_is_a_pure_staging_boundary_without_owner_or_ledger_inputs() -> None:
    batch = parse_trade_republic_csv(_fixture_text("accepted-observed.csv"))

    assert all(
        not hasattr(candidate, field)
        for row in batch.rows
        for candidate in row.candidates
        for field in ("owner_id", "account_id", "id")
    )
