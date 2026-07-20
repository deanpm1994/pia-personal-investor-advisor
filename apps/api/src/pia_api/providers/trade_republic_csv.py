"""Strict, validation-only adapter for the documented Trade Republic CSV v1 dialect."""

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import ConfigDict, Field

from pia_api.domain.financial_events import (
    CashLeg,
    EventLeg,
    FinancialContract,
    FinancialEventType,
    InstrumentLeg,
    Money,
    MovementDirection,
    Quantity,
    SourceIdentity,
    SourceReportedEurEvidence,
)

FORMAT_VERSION = "trade-republic-csv-v1"
PROVIDER_NAME = "trade-republic"

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

DIAGNOSTIC_MISSING_HEADER = "TRCSV001_MISSING_HEADER"
DIAGNOSTIC_REORDERED_HEADER = "TRCSV002_REORDERED_HEADER"
DIAGNOSTIC_UNKNOWN_HEADER = "TRCSV003_UNKNOWN_HEADER"
DIAGNOSTIC_BLANK_TRANSACTION_ID = "TRCSV004_BLANK_TRANSACTION_ID"
DIAGNOSTIC_INVALID_DATETIME = "TRCSV005_INVALID_DATETIME"
DIAGNOSTIC_DATE_MISMATCH = "TRCSV006_DATE_MISMATCH"
DIAGNOSTIC_INVALID_DECIMAL = "TRCSV007_INVALID_DECIMAL"
DIAGNOSTIC_INVALID_SIGN = "TRCSV008_INVALID_SIGN"
DIAGNOSTIC_INCOMPLETE_FX_TRIPLE = "TRCSV009_INCOMPLETE_FX_TRIPLE"
DIAGNOSTIC_MISSING_SECURITY_IDENTIFIER = "TRCSV010_MISSING_SECURITY_IDENTIFIER"
DIAGNOSTIC_MISSING_SHARES = "TRCSV011_MISSING_SHARES"
DIAGNOSTIC_FEE_OR_TAX_SIGN = "TRCSV012_FEE_OR_TAX_SIGN"
DIAGNOSTIC_UNSUPPORTED_SOURCE_TYPE = "TRCSV013_UNSUPPORTED_SOURCE_TYPE"
DIAGNOSTIC_DUPLICATE_SOURCE_IDENTITY = "TRCSV014_DUPLICATE_SOURCE_IDENTITY"
DIAGNOSTIC_ROW_SHAPE = "TRCSV015_ROW_SHAPE"
DIAGNOSTIC_INVALID_CURRENCY = "TRCSV016_INVALID_CURRENCY"
DIAGNOSTIC_INVALID_FX_EVIDENCE = "TRCSV017_INVALID_FX_EVIDENCE"

_DECIMAL = re.compile(r"^-?\d+(?:\.\d+)?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_UTC_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")

_BASE_EVENT_MAPPING: dict[
    str, tuple[FinancialEventType, MovementDirection, MovementDirection | None]
] = {
    "CUSTOMER_INBOUND": (FinancialEventType.DEPOSIT, MovementDirection.IN, None),
    "TRANSFER_INBOUND": (FinancialEventType.DEPOSIT, MovementDirection.IN, None),
    "TRANSFER_INSTANT_INBOUND": (
        FinancialEventType.DEPOSIT,
        MovementDirection.IN,
        None,
    ),
    "TRANSFER_OUTBOUND": (FinancialEventType.WITHDRAWAL, MovementDirection.OUT, None),
    "TRANSFER_INSTANT_OUTBOUND": (
        FinancialEventType.WITHDRAWAL,
        MovementDirection.OUT,
        None,
    ),
    "BUY": (FinancialEventType.BUY, MovementDirection.OUT, MovementDirection.IN),
    "SELL": (FinancialEventType.SELL, MovementDirection.IN, MovementDirection.OUT),
    "DIVIDEND": (FinancialEventType.DIVIDEND, MovementDirection.IN, None),
    "INTEREST_PAYMENT": (FinancialEventType.INTEREST, MovementDirection.IN, None),
}


class TradeRepublicCsvDiagnostic(FinancialContract):
    """An actionable parser diagnostic tied to a source row where available."""

    code: str
    message: str
    row_number: int | None = Field(default=None, ge=1)


class TradeRepublicCsvStagedEvent(FinancialContract):
    """A validated candidate, deliberately not a canonical ledger event."""

    source_identity: SourceIdentity
    event_type: FinancialEventType
    occurred_at: datetime
    legs: tuple[EventLeg, ...] = Field(min_length=1)
    source_reported_eur: SourceReportedEurEvidence | None = None


class TradeRepublicCsvStagedRow(FinancialContract):
    """Source-preserving parsed row and its candidate events or diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    row_number: int = Field(ge=1)
    source_row: dict[str, str]
    candidates: tuple[TradeRepublicCsvStagedEvent, ...] = ()
    diagnostics: tuple[TradeRepublicCsvDiagnostic, ...] = ()


class TradeRepublicCsvStagedBatch(FinancialContract):
    """The complete validation result used to decide future confirmation eligibility."""

    format_version: Literal["trade-republic-csv-v1"] = FORMAT_VERSION
    rows: tuple[TradeRepublicCsvStagedRow, ...] = ()
    diagnostics: tuple[TradeRepublicCsvDiagnostic, ...] = ()

    @property
    def confirmation_eligible(self) -> bool:
        """Allow confirmation only when every source row and file check passed."""
        return not self.diagnostics and all(not row.diagnostics for row in self.rows)


def parse_trade_republic_csv(
    source: str | bytes,
) -> TradeRepublicCsvStagedBatch:
    """Parse only the documented dialect into staged candidates without persistence."""
    if isinstance(source, bytes):
        source = source.decode("utf-8-sig")
    if not isinstance(source, str):
        raise TypeError("source must be UTF-8 CSV text or bytes")

    try:
        records = list(csv.reader(io.StringIO(source, newline="")))
    except csv.Error as error:
        return TradeRepublicCsvStagedBatch(
            diagnostics=(
                TradeRepublicCsvDiagnostic(
                    code=DIAGNOSTIC_ROW_SHAPE,
                    message=f"CSV syntax is invalid: {error}",
                ),
            )
        )

    header_diagnostic = _validate_header(records[0] if records else [])
    if header_diagnostic is not None:
        return TradeRepublicCsvStagedBatch(diagnostics=(header_diagnostic,))

    seen_references: set[str] = set()
    staged_rows: list[TradeRepublicCsvStagedRow] = []
    for row_number, values in enumerate(records[1:], start=2):
        staged_row = _parse_row(row_number, values)
        if staged_row.candidates:
            references = [
                candidate.source_identity.event_reference
                for candidate in staged_row.candidates
            ]
            duplicate_reference = next(
                (reference for reference in references if reference in seen_references),
                None,
            )
            if duplicate_reference is not None:
                staged_row = staged_row.model_copy(
                    update={
                        "candidates": (),
                        "diagnostics": (
                            TradeRepublicCsvDiagnostic(
                                code=DIAGNOSTIC_DUPLICATE_SOURCE_IDENTITY,
                                message=(
                                    "source event reference "
                                    f"{duplicate_reference!r} duplicates an earlier row"
                                ),
                                row_number=row_number,
                            ),
                        ),
                    }
                )
            else:
                seen_references.update(references)
        staged_rows.append(staged_row)

    return TradeRepublicCsvStagedBatch(rows=tuple(staged_rows))


def _validate_header(header: list[str]) -> TradeRepublicCsvDiagnostic | None:
    if not header:
        return TradeRepublicCsvDiagnostic(
            code=DIAGNOSTIC_MISSING_HEADER,
            message="CSV file has no header row",
        )
    if tuple(header) == HEADER:
        return None
    if len(header) == len(HEADER) and set(header) == set(HEADER):
        return TradeRepublicCsvDiagnostic(
            code=DIAGNOSTIC_REORDERED_HEADER,
            message=(
                "CSV header contains the required columns but not in the required order"
            ),
        )
    return TradeRepublicCsvDiagnostic(
        code=DIAGNOSTIC_UNKNOWN_HEADER,
        message="CSV header is missing, duplicated, or contains an unknown column",
    )


def _parse_row(row_number: int, values: list[str]) -> TradeRepublicCsvStagedRow:
    if len(values) != len(HEADER):
        return TradeRepublicCsvStagedRow(
            row_number=row_number,
            source_row={},
            diagnostics=(
                TradeRepublicCsvDiagnostic(
                    code=DIAGNOSTIC_ROW_SHAPE,
                    message=f"row has {len(values)} fields; expected {len(HEADER)}",
                    row_number=row_number,
                ),
            ),
        )
    row = dict(zip(HEADER, values, strict=True))
    diagnostics: list[TradeRepublicCsvDiagnostic] = []
    transaction_id = row["transaction_id"]
    if not transaction_id.strip():
        diagnostics.append(_diagnostic(DIAGNOSTIC_BLANK_TRANSACTION_ID, row_number))

    occurred_at = _parse_datetime(row["datetime"], row_number, diagnostics)
    _validate_date(row["date"], occurred_at, row_number, diagnostics)
    event_mapping = _BASE_EVENT_MAPPING.get(row["type"])
    if event_mapping is None:
        diagnostics.append(
            _diagnostic(
                DIAGNOSTIC_UNSUPPORTED_SOURCE_TYPE,
                row_number,
                f"source type {row['type']!r} is not supported by {FORMAT_VERSION}",
            )
        )
        return TradeRepublicCsvStagedRow(
            row_number=row_number,
            source_row=row,
            diagnostics=tuple(diagnostics),
        )

    decimals = _parse_decimal_columns(row, row_number, diagnostics)
    _validate_currency_fields(row, row_number, diagnostics)
    fx_present = _validate_fx_triple(
        row, decimals, event_mapping, row_number, diagnostics
    )

    _validate_event_semantics(
        row, decimals, event_mapping, fx_present, row_number, diagnostics
    )

    if diagnostics:
        return TradeRepublicCsvStagedRow(
            row_number=row_number,
            source_row=row,
            diagnostics=tuple(diagnostics),
        )
    assert occurred_at is not None
    return TradeRepublicCsvStagedRow(
        row_number=row_number,
        source_row=row,
        candidates=_build_candidates(
            row, decimals, event_mapping, occurred_at, fx_present
        ),
    )


def _parse_datetime(
    value: str,
    row_number: int,
    diagnostics: list[TradeRepublicCsvDiagnostic],
) -> datetime | None:
    if not _UTC_ISO_DATETIME.fullmatch(value):
        diagnostics.append(_diagnostic(DIAGNOSTIC_INVALID_DATETIME, row_number))
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        diagnostics.append(_diagnostic(DIAGNOSTIC_INVALID_DATETIME, row_number))
        return None


def _validate_date(
    value: str,
    occurred_at: datetime | None,
    row_number: int,
    diagnostics: list[TradeRepublicCsvDiagnostic],
) -> None:
    try:
        reported_date = date.fromisoformat(value)
    except ValueError:
        diagnostics.append(_diagnostic(DIAGNOSTIC_DATE_MISMATCH, row_number))
        return
    if occurred_at is not None and reported_date != occurred_at.date():
        diagnostics.append(_diagnostic(DIAGNOSTIC_DATE_MISMATCH, row_number))


def _parse_decimal_columns(
    row: dict[str, str],
    row_number: int,
    diagnostics: list[TradeRepublicCsvDiagnostic],
) -> dict[str, Decimal | None]:
    parsed: dict[str, Decimal | None] = {}
    for column in (
        "shares",
        "price",
        "amount",
        "fee",
        "tax",
        "original_amount",
        "fx_rate",
    ):
        value = row[column]
        if not value and column != "amount":
            parsed[column] = None
            continue
        if not value or not _DECIMAL.fullmatch(value):
            diagnostics.append(
                _diagnostic(
                    DIAGNOSTIC_INVALID_DECIMAL,
                    row_number,
                    f"{column} must be a finite dot-decimal value",
                )
            )
            parsed[column] = None
            continue
        try:
            decimal_value = Decimal(value)
        except InvalidOperation:
            diagnostics.append(_diagnostic(DIAGNOSTIC_INVALID_DECIMAL, row_number))
            parsed[column] = None
            continue
        if not decimal_value.is_finite():
            diagnostics.append(_diagnostic(DIAGNOSTIC_INVALID_DECIMAL, row_number))
            parsed[column] = None
            continue
        parsed[column] = decimal_value
    return parsed


def _validate_currency_fields(
    row: dict[str, str],
    row_number: int,
    diagnostics: list[TradeRepublicCsvDiagnostic],
) -> None:
    if not _CURRENCY.fullmatch(row["currency"]):
        diagnostics.append(
            _diagnostic(
                DIAGNOSTIC_INVALID_CURRENCY,
                row_number,
                "currency must be an ISO-style three-letter uppercase code",
            )
        )
    if row["original_currency"] and not _CURRENCY.fullmatch(row["original_currency"]):
        diagnostics.append(
            _diagnostic(
                DIAGNOSTIC_INVALID_CURRENCY,
                row_number,
                "original_currency must be an ISO-style three-letter uppercase code",
            )
        )


def _validate_fx_triple(
    row: dict[str, str],
    decimals: dict[str, Decimal | None],
    event_mapping: tuple[
        FinancialEventType, MovementDirection, MovementDirection | None
    ]
    | None,
    row_number: int,
    diagnostics: list[TradeRepublicCsvDiagnostic],
) -> bool:
    fields = ("original_amount", "original_currency", "fx_rate")
    populated = [bool(row[field]) for field in fields]
    if any(populated) and not all(populated):
        diagnostics.append(_diagnostic(DIAGNOSTIC_INCOMPLETE_FX_TRIPLE, row_number))
        return False
    if not all(populated):
        return False
    if row["currency"] != "EUR":
        diagnostics.append(
            _diagnostic(
                DIAGNOSTIC_INVALID_FX_EVIDENCE,
                row_number,
                "source-reported FX evidence requires amount currency EUR",
            )
        )
    if decimals["fx_rate"] is not None and decimals["fx_rate"] <= 0:
        diagnostics.append(
            _diagnostic(
                DIAGNOSTIC_INVALID_FX_EVIDENCE,
                row_number,
                "source-reported FX rate must be greater than zero",
            )
        )
    if event_mapping is not None:
        direction = event_mapping[1]
        original_amount = decimals["original_amount"]
        if original_amount is not None and not _has_expected_sign(
            original_amount, direction
        ):
            diagnostics.append(
                _diagnostic(
                    DIAGNOSTIC_INVALID_SIGN,
                    row_number,
                    "original_amount has an invalid sign for the source type",
                )
            )
    return True


def _validate_event_semantics(
    row: dict[str, str],
    decimals: dict[str, Decimal | None],
    event_mapping: tuple[
        FinancialEventType, MovementDirection, MovementDirection | None
    ],
    fx_present: bool,
    row_number: int,
    diagnostics: list[TradeRepublicCsvDiagnostic],
) -> None:
    _, cash_direction, instrument_direction = event_mapping
    amount = decimals["amount"]
    if amount is not None and not _has_expected_sign(amount, cash_direction):
        diagnostics.append(_diagnostic(DIAGNOSTIC_INVALID_SIGN, row_number))
    if instrument_direction is not None:
        if not row["symbol"].strip():
            diagnostics.append(
                _diagnostic(DIAGNOSTIC_MISSING_SECURITY_IDENTIFIER, row_number)
            )
        shares = decimals["shares"]
        if shares is None or shares <= 0:
            diagnostics.append(_diagnostic(DIAGNOSTIC_MISSING_SHARES, row_number))
    for component in ("fee", "tax"):
        value = decimals[component]
        if value is not None and value > 0:
            diagnostics.append(
                _diagnostic(
                    DIAGNOSTIC_FEE_OR_TAX_SIGN,
                    row_number,
                    f"{component} must be negative when nonzero",
                )
            )
    if fx_present and decimals["original_amount"] is None:
        # The lexical triple was complete but the numeric source amount was invalid.
        return


def _has_expected_sign(value: Decimal, direction: MovementDirection) -> bool:
    return value > 0 if direction is MovementDirection.IN else value < 0


def _build_candidates(
    row: dict[str, str],
    decimals: dict[str, Decimal | None],
    event_mapping: tuple[
        FinancialEventType, MovementDirection, MovementDirection | None
    ],
    occurred_at: datetime,
    fx_present: bool,
) -> tuple[TradeRepublicCsvStagedEvent, ...]:
    event_type, cash_direction, instrument_direction = event_mapping
    amount = decimals["original_amount"] if fx_present else decimals["amount"]
    currency = row["original_currency"] if fx_present else row["currency"]
    assert amount is not None
    legs: list[EventLeg] = [
        CashLeg(
            direction=cash_direction, money=Money(amount=abs(amount), currency=currency)
        )
    ]
    if instrument_direction is not None:
        shares = decimals["shares"]
        assert shares is not None
        legs.append(
            InstrumentLeg(
                direction=instrument_direction,
                instrument_id=row["symbol"],
                quantity=Quantity(value=shares),
            )
        )
    source_reported_eur = None
    if fx_present:
        eur_amount = decimals["amount"]
        fx_rate = decimals["fx_rate"]
        assert eur_amount is not None and fx_rate is not None
        source_reported_eur = SourceReportedEurEvidence(
            eur_amount=Money(amount=abs(eur_amount), currency="EUR"),
            source_rate=fx_rate,
            reported_at=occurred_at,
        )
    candidates = [
        TradeRepublicCsvStagedEvent(
            source_identity=SourceIdentity(
                provider=PROVIDER_NAME,
                event_reference=f"{row['transaction_id']}:base",
            ),
            event_type=event_type,
            occurred_at=occurred_at,
            legs=tuple(legs),
            source_reported_eur=source_reported_eur,
        )
    ]
    for column, component, component_event_type in (
        ("fee", "fee", FinancialEventType.FEE),
        ("tax", "withholding-tax", FinancialEventType.WITHHOLDING_TAX),
    ):
        value = decimals[column]
        if value is not None and value != 0:
            candidates.append(
                TradeRepublicCsvStagedEvent(
                    source_identity=SourceIdentity(
                        provider=PROVIDER_NAME,
                        event_reference=f"{row['transaction_id']}:{component}",
                    ),
                    event_type=component_event_type,
                    occurred_at=occurred_at,
                    legs=(
                        CashLeg(
                            direction=MovementDirection.OUT,
                            money=Money(amount=abs(value), currency=row["currency"]),
                        ),
                    ),
                )
            )
    return tuple(candidates)


def _diagnostic(
    code: str, row_number: int, message: str | None = None
) -> TradeRepublicCsvDiagnostic:
    return TradeRepublicCsvDiagnostic(
        code=code,
        message=message or _DEFAULT_MESSAGES[code],
        row_number=row_number,
    )


_DEFAULT_MESSAGES = {
    DIAGNOSTIC_BLANK_TRANSACTION_ID: "transaction_id must not be blank",
    DIAGNOSTIC_INVALID_DATETIME: (
        "datetime must be a UTC ISO-8601 timestamp ending in Z"
    ),
    DIAGNOSTIC_DATE_MISMATCH: "date must match datetime's UTC calendar date",
    DIAGNOSTIC_INVALID_DECIMAL: "numeric fields must be finite dot-decimal values",
    DIAGNOSTIC_INVALID_SIGN: "amount must be nonzero and have the required sign",
    DIAGNOSTIC_INCOMPLETE_FX_TRIPLE: (
        "original_amount, original_currency, and fx_rate must be all present or absent"
    ),
    DIAGNOSTIC_MISSING_SECURITY_IDENTIFIER: "BUY and SELL rows require symbol",
    DIAGNOSTIC_MISSING_SHARES: "BUY and SELL rows require positive shares",
    DIAGNOSTIC_FEE_OR_TAX_SIGN: "nonzero fee and tax values must be negative",
}
