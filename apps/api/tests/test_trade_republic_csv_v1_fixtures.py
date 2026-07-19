"""Contract tests for the static Trade Republic CSV v1 mapping corpus.

These tests intentionally do not parse provider files or construct ledger events.
P4.3 will implement that runtime boundary against this versioned corpus.
"""

import csv
import json
from pathlib import Path

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "trade_republic_csv_v1"
MAPPING_DOCUMENT = (
    Path(__file__).parents[3] / "docs" / "imports" / "trade-republic-csv-v1.md"
)
HEADER = (
    "datetime",
    "date",
    "account_type",
    "category",
    "type",
    "asset_class",
    "name",
    "symbol",
    "shares",
    "price",
    "amount",
    "fee",
    "tax",
    "currency",
    "original_amount",
    "original_currency",
    "fx_rate",
    "description",
    "transaction_id",
    "counterparty_name",
    "counterparty_iban",
    "payment_reference",
    "mcc_code",
)
DIAGNOSTIC_CODES = {
    "TRCSV001_MISSING_HEADER",
    "TRCSV002_REORDERED_HEADER",
    "TRCSV003_UNKNOWN_HEADER",
    "TRCSV004_BLANK_TRANSACTION_ID",
    "TRCSV005_INVALID_DATETIME",
    "TRCSV006_DATE_MISMATCH",
    "TRCSV007_INVALID_DECIMAL",
    "TRCSV008_INVALID_SIGN",
    "TRCSV009_INCOMPLETE_FX_TRIPLE",
    "TRCSV010_MISSING_SECURITY_IDENTIFIER",
    "TRCSV011_MISSING_SHARES",
    "TRCSV012_FEE_OR_TAX_SIGN",
    "TRCSV013_UNSUPPORTED_SOURCE_TYPE",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fixture_file:
        return list(csv.DictReader(fixture_file))


def _detect_header(path: Path) -> str | None:
    with path.open(encoding="utf-8-sig", newline="") as fixture_file:
        header = next(csv.reader(fixture_file), [])
    if not header:
        return "TRCSV001_MISSING_HEADER"
    if set(header) != set(HEADER):
        return "TRCSV003_UNKNOWN_HEADER"
    if tuple(header) != HEADER:
        return "TRCSV002_REORDERED_HEADER"
    return None


def _manifest() -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / "expected-events.json").read_text())


def test_header_detection_is_versioned_and_tolerates_a_utf8_bom() -> None:
    assert _detect_header(FIXTURE_ROOT / "accepted-observed.csv") is None
    assert _detect_header(FIXTURE_ROOT / "extension-phase3-test-only.csv") is None
    assert _detect_header(FIXTURE_ROOT / "malformed" / "missing-header.csv") == (
        "TRCSV001_MISSING_HEADER"
    )
    assert _detect_header(FIXTURE_ROOT / "malformed" / "reordered-header.csv") == (
        "TRCSV002_REORDERED_HEADER"
    )


def test_fixtures_use_observed_header_except_declared_failures() -> None:
    malformed = _manifest()["malformed"]
    declared_header_failures = {
        item["fixture"]
        for item in malformed
        if item["diagnostic_code"]
        in {
            "TRCSV001_MISSING_HEADER",
            "TRCSV002_REORDERED_HEADER",
            "TRCSV003_UNKNOWN_HEADER",
        }
    }
    for fixture_path in FIXTURE_ROOT.rglob("*.csv"):
        relative_path = str(fixture_path.relative_to(FIXTURE_ROOT))
        if relative_path not in declared_header_failures:
            assert _detect_header(fixture_path) is None, relative_path


def test_declared_data_rows_preserve_the_23_column_shape() -> None:
    for fixture_path in FIXTURE_ROOT.rglob("*.csv"):
        if _detect_header(fixture_path) is not None:
            continue
        with fixture_path.open(encoding="utf-8-sig", newline="") as fixture_file:
            rows = list(csv.reader(fixture_file))
        assert all(len(row) == len(HEADER) for row in rows[1:]), fixture_path


def test_manifest_covers_all_phase_three_event_categories() -> None:
    manifest = _manifest()
    observed_events = {
        event["event_type"] for event in manifest["observed_expected_events"]
    }
    extension_events = {
        event["event_type"] for event in manifest["extension_expected_events"]
    }
    assert observed_events == {
        "deposit",
        "withdrawal",
        "buy",
        "sell",
        "dividend",
        "interest",
        "fee",
        "withholding_tax",
    }
    assert extension_events == {
        "source_reported_fx_conversion",
        "stock_split",
        "correction",
        "reversal",
    }


def test_manifest_component_references_are_deterministic_and_complete() -> None:
    manifest = _manifest()
    expected_events = (
        manifest["observed_expected_events"] + manifest["extension_expected_events"]
    )
    seen_references: set[str] = set()
    for event in expected_events:
        assert event["event_reference"] == (
            f"{event['transaction_id']}:{event['component']}"
        )
        assert event["event_reference"] not in seen_references
        seen_references.add(event["event_reference"])

    source_rows = _read_csv(FIXTURE_ROOT / "accepted-observed.csv")
    source_ids = {row["transaction_id"] for row in source_rows}
    for event in manifest["observed_expected_events"]:
        assert event["transaction_id"] in source_ids


def test_fixture_values_are_synthetic_and_sensitive_columns_are_blank() -> None:
    for fixture_path in FIXTURE_ROOT.rglob("*.csv"):
        if _detect_header(fixture_path) is not None:
            continue
        for row in _read_csv(fixture_path):
            assert not row["transaction_id"] or row["transaction_id"].startswith(
                "synthetic-"
            )
            assert row["counterparty_name"] == ""
            assert row["counterparty_iban"] == ""
            assert row["payment_reference"] == ""
            assert not row["name"] or row["name"].startswith("Synthetic ")
            assert not row["symbol"] or row["symbol"].startswith("SYNTH-")


def test_manifest_declares_every_expected_diagnostic_code() -> None:
    manifest = _manifest()
    expected_codes = {item["diagnostic_code"] for item in manifest["malformed"]}
    assert expected_codes == DIAGNOSTIC_CODES
    document = MAPPING_DOCUMENT.read_text()
    for code in DIAGNOSTIC_CODES:
        assert code in document
