"""Hand-worked, synthetic ledger history shared by P3.4 invariant tests."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from pia_api.domain.financial_events import (
    CashLeg,
    FinancialEvent,
    FinancialEventType,
    InstrumentLeg,
    Money,
    MovementDirection,
    Quantity,
    SourceIdentity,
    SourceReportedEurEvidence,
)

OWNER_ID = UUID("00000000-0000-0000-0000-000000000101")
ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000201")
INSTRUMENT_ID = "US0378331005"


@dataclass(frozen=True)
class FinancialFixtureEvent:
    """A persisted-ledger identifier paired with its immutable event contract."""

    event_id: UUID
    event: FinancialEvent


@dataclass(frozen=True)
class FifoAllocationFixture:
    """Hand-worked expected allocation, not an accounting implementation."""

    source_event_reference: str
    quantity: Decimal
    allocated_basis: Decimal


def _cash(direction: MovementDirection, amount: str, currency: str = "EUR") -> CashLeg:
    return CashLeg(direction=direction, money=Money(amount=amount, currency=currency))


def _instrument(direction: MovementDirection, quantity: str) -> InstrumentLeg:
    return InstrumentLeg(
        direction=direction,
        instrument_id=INSTRUMENT_ID,
        quantity=Quantity(value=quantity),
    )


def _fixture_event(
    event_id: str,
    source_reference: str,
    event_type: FinancialEventType,
    legs: list[CashLeg | InstrumentLeg],
    *,
    source_reported_eur: SourceReportedEurEvidence | None = None,
    correction_of_event_id: UUID | None = None,
    reversal_of_event_id: UUID | None = None,
) -> FinancialFixtureEvent:
    return FinancialFixtureEvent(
        event_id=UUID(event_id),
        event=FinancialEvent(
            owner_id=OWNER_ID,
            account_id=ACCOUNT_ID,
            source_identity=SourceIdentity(
                provider="synthetic-fixture", event_reference=source_reference
            ),
            event_type=event_type,
            occurred_at=datetime(2026, 7, 16, 9, 0, tzinfo=UTC),
            legs=legs,
            source_reported_eur=source_reported_eur,
            correction_of_event_id=correction_of_event_id,
            reversal_of_event_id=reversal_of_event_id,
        ),
    )


DEPOSIT = _fixture_event(
    "00000000-0000-0000-0000-000000000301",
    "deposit-1",
    FinancialEventType.DEPOSIT,
    [_cash(MovementDirection.IN, "1000.0000")],
)
WITHDRAWAL = _fixture_event(
    "00000000-0000-0000-0000-000000000302",
    "withdrawal-1",
    FinancialEventType.WITHDRAWAL,
    [_cash(MovementDirection.OUT, "20.005")],
)
FIRST_BUY = _fixture_event(
    "00000000-0000-0000-0000-000000000303",
    "buy-1",
    FinancialEventType.BUY,
    [
        _cash(MovementDirection.OUT, "100.000"),
        _instrument(MovementDirection.IN, "1.250"),
    ],
)
PURCHASE_FEE = _fixture_event(
    "00000000-0000-0000-0000-000000000304",
    "purchase-fee-1",
    FinancialEventType.FEE,
    [_cash(MovementDirection.OUT, "1.000")],
)
SECOND_BUY = _fixture_event(
    "00000000-0000-0000-0000-000000000305",
    "buy-2",
    FinancialEventType.BUY,
    [
        _cash(MovementDirection.OUT, "50.000"),
        _instrument(MovementDirection.IN, "2.500"),
    ],
)
SECOND_PURCHASE_FEE = _fixture_event(
    "00000000-0000-0000-0000-000000000306",
    "purchase-fee-2",
    FinancialEventType.FEE,
    [_cash(MovementDirection.OUT, "0.500")],
)
PARTIAL_SALE = _fixture_event(
    "00000000-0000-0000-0000-000000000307",
    "sell-1",
    FinancialEventType.SELL,
    [
        _cash(MovementDirection.IN, "150.005"),
        _instrument(MovementDirection.OUT, "2.000"),
    ],
)
SALE_FEE = _fixture_event(
    "00000000-0000-0000-0000-000000000308",
    "sale-fee-1",
    FinancialEventType.FEE,
    [_cash(MovementDirection.OUT, "0.995")],
)
DIVIDEND = _fixture_event(
    "00000000-0000-0000-0000-000000000309",
    "dividend-1",
    FinancialEventType.DIVIDEND,
    [_cash(MovementDirection.IN, "10.005")],
)
WITHHOLDING_TAX = _fixture_event(
    "00000000-0000-0000-0000-000000000310",
    "withholding-tax-1",
    FinancialEventType.WITHHOLDING_TAX,
    [_cash(MovementDirection.OUT, "1.505")],
)
INTEREST = _fixture_event(
    "00000000-0000-0000-0000-000000000311",
    "interest-1",
    FinancialEventType.INTEREST,
    [_cash(MovementDirection.IN, "0.0100")],
)
SOURCE_REPORTED_FX = _fixture_event(
    "00000000-0000-0000-0000-000000000312",
    "fx-1",
    FinancialEventType.SOURCE_REPORTED_FX_CONVERSION,
    [
        _cash(MovementDirection.OUT, "10.0000", "USD"),
        _cash(MovementDirection.IN, "9.2000"),
    ],
    source_reported_eur=SourceReportedEurEvidence(
        eur_amount=Money(amount="9.2000", currency="EUR"),
        source_rate="0.9200",
        reported_at=datetime(2026, 7, 16, 9, 1, tzinfo=UTC),
    ),
)
STOCK_SPLIT = _fixture_event(
    "00000000-0000-0000-0000-000000000313",
    "split-1",
    FinancialEventType.STOCK_SPLIT,
    [
        _instrument(MovementDirection.OUT, "1.750"),
        _instrument(MovementDirection.IN, "3.500"),
    ],
)
CORRECTION = _fixture_event(
    "00000000-0000-0000-0000-000000000314",
    "correction-1",
    FinancialEventType.CORRECTION,
    [_cash(MovementDirection.IN, "0.0100")],
    correction_of_event_id=DEPOSIT.event_id,
)
REVERSAL = _fixture_event(
    "00000000-0000-0000-0000-000000000315",
    "reversal-1",
    FinancialEventType.REVERSAL,
    [_cash(MovementDirection.OUT, "0.0100")],
    reversal_of_event_id=DEPOSIT.event_id,
)

FIXTURE_HISTORY = (
    DEPOSIT,
    WITHDRAWAL,
    FIRST_BUY,
    PURCHASE_FEE,
    SECOND_BUY,
    SECOND_PURCHASE_FEE,
    PARTIAL_SALE,
    SALE_FEE,
    DIVIDEND,
    WITHHOLDING_TAX,
    INTEREST,
    SOURCE_REPORTED_FX,
    STOCK_SPLIT,
    CORRECTION,
    REVERSAL,
)

DUPLICATE_DEPOSIT = _fixture_event(
    "00000000-0000-0000-0000-000000000316",
    DEPOSIT.event.source_identity.event_reference,
    FinancialEventType.DEPOSIT,
    [_cash(MovementDirection.IN, "1000.0000")],
)

PARTIAL_SALE_FIFO_ALLOCATIONS = (
    FifoAllocationFixture("buy-1", Decimal("1.250"), Decimal("101.000")),
    FifoAllocationFixture("buy-2", Decimal("0.750"), Decimal("15.150")),
)
PARTIAL_SALE_REMAINING_QUANTITY = Decimal("1.750")
