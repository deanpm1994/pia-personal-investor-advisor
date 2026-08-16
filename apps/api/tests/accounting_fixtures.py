"""Hand-worked Phase 5 accounting oracle; this module is fixture data only.

It deliberately does not replay events or calculate balances. Production accounting
is introduced by later approved Phase 5 issues.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from financial_fixtures import (
    ACCOUNT_ID,
    CORRECTION,
    DEPOSIT,
    FIRST_BUY,
    FIXTURE_HISTORY,
    INSTRUMENT_ID,
    OWNER_ID,
    PARTIAL_SALE,
    PARTIAL_SALE_FIFO_ALLOCATIONS,
    PURCHASE_FEE,
    REVERSAL,
    SALE_FEE,
    SECOND_BUY,
    SECOND_PURCHASE_FEE,
)

from pia_api.domain.financial_events import (
    CashLeg,
    FinancialEvent,
    FinancialEventType,
    InstrumentLeg,
    Money,
    MovementDirection,
    Quantity,
    SourceIdentity,
)


class AccountRole(StrEnum):
    """P5.2 fixture-only account roles from ADR 0007."""

    BROKERAGE = "brokerage"
    CASH = "cash"
    SAVINGS = "savings"
    EMERGENCY_RESERVE = "emergency_reserve"


@dataclass(frozen=True)
class AccountFixture:
    """Account metadata needed by the later accounting fold."""

    account_id: UUID
    role: AccountRole
    emergency_reserve_target_eur: Decimal | None = None


@dataclass(frozen=True)
class AccountingFixtureEvent:
    """Fixture replay metadata kept outside the Phase 3 event contract."""

    event_id: UUID
    event: FinancialEvent
    created_at: datetime
    source_group_reference: str | None = None
    is_manual_opening_balance: bool = False


@dataclass(frozen=True)
class CashBalanceExpectation:
    """Exact native-currency cash result, transcribed from the reconciliation."""

    account_id: UUID
    currency: str
    amount: Decimal


@dataclass(frozen=True)
class PositionExpectation:
    """Exact post-split position; no market value is implied."""

    account_id: UUID
    instrument_id: str
    quantity: Decimal
    remaining_basis: Decimal


@dataclass(frozen=True)
class FifoLotExpectation:
    """Remaining fractional FIFO lot after the sale and stock split."""

    source_event_reference: str
    quantity: Decimal
    total_basis: Decimal


@dataclass(frozen=True)
class AccountingDiagnosticExpectation:
    """Stable expected diagnostic for unavailable or impossible histories."""

    code: str
    event_id: UUID
    account_id: UUID
    source_group_reference: str | None = None


CASH_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000401")
SAVINGS_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000402")
RESERVE_EUR_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000403")
RESERVE_UNTARGETED_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000404")
RESERVE_USD_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000405")
BROKERAGE_ACCOUNT_ID = ACCOUNT_ID

ACCOUNT_FIXTURES = (
    AccountFixture(BROKERAGE_ACCOUNT_ID, AccountRole.BROKERAGE),
    AccountFixture(CASH_ACCOUNT_ID, AccountRole.CASH),
    AccountFixture(SAVINGS_ACCOUNT_ID, AccountRole.SAVINGS),
    AccountFixture(
        RESERVE_EUR_ACCOUNT_ID,
        AccountRole.EMERGENCY_RESERVE,
        Decimal("500.0000"),
    ),
    AccountFixture(RESERVE_UNTARGETED_ACCOUNT_ID, AccountRole.EMERGENCY_RESERVE),
    AccountFixture(
        RESERVE_USD_ACCOUNT_ID,
        AccountRole.EMERGENCY_RESERVE,
        Decimal("250.0000"),
    ),
)


def _cash_event(
    event_id: str,
    source_reference: str,
    account_id: UUID,
    event_type: FinancialEventType,
    direction: MovementDirection,
    amount: str,
    currency: str,
    occurred_at: datetime,
    *,
    source_group_reference: str | None = None,
    is_manual_opening_balance: bool = False,
    reversal_of_event_id: UUID | None = None,
) -> AccountingFixtureEvent:
    """Build a valid source fact without supplying an accounting result."""
    return AccountingFixtureEvent(
        event_id=UUID(event_id),
        event=FinancialEvent(
            owner_id=OWNER_ID,
            account_id=account_id,
            source_identity=SourceIdentity(
                provider="synthetic-accounting-oracle",
                event_reference=source_reference,
            ),
            event_type=event_type,
            occurred_at=occurred_at,
            legs=[
                CashLeg(
                    direction=direction, money=Money(amount=amount, currency=currency)
                )
            ],
            reversal_of_event_id=reversal_of_event_id,
        ),
        created_at=occurred_at,
        source_group_reference=source_group_reference,
        is_manual_opening_balance=is_manual_opening_balance,
    )


def _brokerage_event(
    fixture_index: int,
    *,
    source_group_reference: str | None = None,
) -> AccountingFixtureEvent:
    """Give the Phase 3 history P5.2 replay ordering and grouping evidence."""
    fixture = FIXTURE_HISTORY[fixture_index]
    return AccountingFixtureEvent(
        event_id=fixture.event_id,
        event=fixture.event,
        created_at=datetime(2026, 7, 16, 9, 0, tzinfo=UTC),
        source_group_reference=source_group_reference,
    )


BROKERAGE_ACCOUNTING_HISTORY = tuple(
    _brokerage_event(
        index,
        source_group_reference={
            FIRST_BUY.event_id: "trade-buy-1",
            PURCHASE_FEE.event_id: "trade-buy-1",
            SECOND_BUY.event_id: "trade-buy-2",
            SECOND_PURCHASE_FEE.event_id: "trade-buy-2",
            PARTIAL_SALE.event_id: "trade-sell-1",
            SALE_FEE.event_id: "trade-sell-1",
        }.get(FIXTURE_HISTORY[index].event_id),
    )
    for index in range(len(FIXTURE_HISTORY))
)

MANUAL_OPENING_CASH = _cash_event(
    "00000000-0000-0000-0000-000000000501",
    "cash-opening",
    CASH_ACCOUNT_ID,
    FinancialEventType.DEPOSIT,
    MovementDirection.IN,
    "100.0000",
    "EUR",
    datetime(2026, 7, 17, 9, 0, tzinfo=UTC),
    is_manual_opening_balance=True,
)
MANUAL_CASH_DEPOSIT = _cash_event(
    "00000000-0000-0000-0000-000000000502",
    "cash-deposit-1",
    CASH_ACCOUNT_ID,
    FinancialEventType.DEPOSIT,
    MovementDirection.IN,
    "25.000",
    "EUR",
    datetime(2026, 7, 17, 10, 0, tzinfo=UTC),
)
MANUAL_CASH_WITHDRAWAL = _cash_event(
    "00000000-0000-0000-0000-000000000503",
    "cash-withdrawal-1",
    CASH_ACCOUNT_ID,
    FinancialEventType.WITHDRAWAL,
    MovementDirection.OUT,
    "10.000",
    "EUR",
    datetime(2026, 7, 17, 11, 0, tzinfo=UTC),
)
TRANSFER_OUT = _cash_event(
    "00000000-0000-0000-0000-000000000504",
    "cash-to-savings-out",
    CASH_ACCOUNT_ID,
    FinancialEventType.WITHDRAWAL,
    MovementDirection.OUT,
    "40.000",
    "EUR",
    datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
    source_group_reference="transfer-cash-savings-1",
)
MANUAL_OPENING_SAVINGS = _cash_event(
    "00000000-0000-0000-0000-000000000505",
    "savings-opening",
    SAVINGS_ACCOUNT_ID,
    FinancialEventType.DEPOSIT,
    MovementDirection.IN,
    "500.0000",
    "EUR",
    datetime(2026, 7, 17, 9, 0, tzinfo=UTC),
    is_manual_opening_balance=True,
)
TRANSFER_IN = _cash_event(
    "00000000-0000-0000-0000-000000000506",
    "cash-to-savings-in",
    SAVINGS_ACCOUNT_ID,
    FinancialEventType.DEPOSIT,
    MovementDirection.IN,
    "40.000",
    "EUR",
    datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
    source_group_reference="transfer-cash-savings-1",
)
MANUAL_OPENING_RESERVE_EUR = _cash_event(
    "00000000-0000-0000-0000-000000000507",
    "reserve-eur-opening",
    RESERVE_EUR_ACCOUNT_ID,
    FinancialEventType.DEPOSIT,
    MovementDirection.IN,
    "300.0000",
    "EUR",
    datetime(2026, 7, 17, 9, 0, tzinfo=UTC),
    is_manual_opening_balance=True,
)
MANUAL_RESERVE_EUR_DEPOSIT = _cash_event(
    "00000000-0000-0000-0000-000000000508",
    "reserve-eur-deposit-1",
    RESERVE_EUR_ACCOUNT_ID,
    FinancialEventType.DEPOSIT,
    MovementDirection.IN,
    "50.000",
    "EUR",
    datetime(2026, 7, 17, 10, 0, tzinfo=UTC),
)
MANUAL_OPENING_UNTARGETED_RESERVE = _cash_event(
    "00000000-0000-0000-0000-000000000509",
    "reserve-untargeted-opening",
    RESERVE_UNTARGETED_ACCOUNT_ID,
    FinancialEventType.DEPOSIT,
    MovementDirection.IN,
    "200.0000",
    "EUR",
    datetime(2026, 7, 17, 9, 0, tzinfo=UTC),
    is_manual_opening_balance=True,
)
MANUAL_OPENING_RESERVE_USD = _cash_event(
    "00000000-0000-0000-0000-000000000510",
    "reserve-usd-opening",
    RESERVE_USD_ACCOUNT_ID,
    FinancialEventType.DEPOSIT,
    MovementDirection.IN,
    "80.0000",
    "USD",
    datetime(2026, 7, 17, 9, 0, tzinfo=UTC),
    is_manual_opening_balance=True,
)

MANUAL_ACCOUNTING_HISTORY = (
    MANUAL_OPENING_CASH,
    MANUAL_CASH_DEPOSIT,
    MANUAL_CASH_WITHDRAWAL,
    TRANSFER_OUT,
    MANUAL_OPENING_SAVINGS,
    TRANSFER_IN,
    MANUAL_OPENING_RESERVE_EUR,
    MANUAL_RESERVE_EUR_DEPOSIT,
    MANUAL_OPENING_UNTARGETED_RESERVE,
    MANUAL_OPENING_RESERVE_USD,
)
ACCOUNTING_FIXTURE_HISTORY = BROKERAGE_ACCOUNTING_HISTORY + MANUAL_ACCOUNTING_HISTORY

# Every Phase 3 brokerage fact has identical occurred/created timestamps. UUID
# textual order is therefore the required and hand-worked final tie breaker.
EXPECTED_BROKERAGE_TIE_BREAK_ORDER = tuple(
    fixture.event.source_identity.event_reference for fixture in FIXTURE_HISTORY
)

CASH_BALANCE_EXPECTATIONS = (
    CashBalanceExpectation(BROKERAGE_ACCOUNT_ID, "EUR", Decimal("995.2150")),
    CashBalanceExpectation(BROKERAGE_ACCOUNT_ID, "USD", Decimal("-10.0000")),
    CashBalanceExpectation(CASH_ACCOUNT_ID, "EUR", Decimal("75.0000")),
    CashBalanceExpectation(SAVINGS_ACCOUNT_ID, "EUR", Decimal("540.0000")),
    CashBalanceExpectation(RESERVE_EUR_ACCOUNT_ID, "EUR", Decimal("350.0000")),
    CashBalanceExpectation(RESERVE_UNTARGETED_ACCOUNT_ID, "EUR", Decimal("200.0000")),
    CashBalanceExpectation(RESERVE_USD_ACCOUNT_ID, "USD", Decimal("80.0000")),
)

EXPECTED_OWNER_NATIVE_CASH = {
    "EUR": Decimal("2160.2150"),
    "USD": Decimal("70.0000"),
}
EXPECTED_EUR_OWNER_AGGREGATE = None

POSITION_EXPECTATIONS = (
    PositionExpectation(
        BROKERAGE_ACCOUNT_ID,
        INSTRUMENT_ID,
        Decimal("3.500"),
        Decimal("35.350"),
    ),
)
FIFO_REMAINING_LOTS = (
    FifoLotExpectation("buy-2", Decimal("3.500"), Decimal("35.350")),
)
EXPECTED_FEE_ADJUSTED_PROCEEDS = Decimal("149.010")
EXPECTED_REALIZED_GAIN = Decimal("32.860")
EXPECTED_RESERVE_EUR_BALANCE = Decimal("350.0000")
EXPECTED_RESERVE_EUR_TARGET = Decimal("500.0000")
EXPECTED_RESERVE_EUR_PROGRESS = Decimal("0.7000")

INVALID_OVERSELL = AccountingFixtureEvent(
    event_id=UUID("00000000-0000-0000-0000-000000000601"),
    event=FinancialEvent(
        owner_id=OWNER_ID,
        account_id=BROKERAGE_ACCOUNT_ID,
        source_identity=SourceIdentity(
            provider="synthetic-accounting-oracle", event_reference="oversell-1"
        ),
        event_type=FinancialEventType.SELL,
        occurred_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
        legs=[
            CashLeg(
                direction=MovementDirection.IN,
                money=Money(amount="1.000", currency="EUR"),
            ),
            InstrumentLeg(
                direction=MovementDirection.OUT,
                instrument_id=INSTRUMENT_ID,
                quantity=Quantity(value="4.000"),
            ),
        ],
    ),
    created_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
    source_group_reference="trade-oversell-1",
)
INVALID_NON_NEGATING_REVERSAL = _cash_event(
    "00000000-0000-0000-0000-000000000602",
    "invalid-reversal-1",
    BROKERAGE_ACCOUNT_ID,
    FinancialEventType.REVERSAL,
    MovementDirection.OUT,
    "1.0000",
    "EUR",
    datetime(2026, 7, 18, 10, 0, tzinfo=UTC),
    reversal_of_event_id=DEPOSIT.event_id,
)

INCOMPLETE_DIAGNOSTIC_EXPECTATIONS = (
    AccountingDiagnosticExpectation(
        "ACCOUNTING_NON_EUR_AGGREGATION_UNAVAILABLE",
        MANUAL_OPENING_RESERVE_USD.event_id,
        RESERVE_USD_ACCOUNT_ID,
    ),
    AccountingDiagnosticExpectation(
        "ACCOUNTING_OVERSELL",
        INVALID_OVERSELL.event_id,
        BROKERAGE_ACCOUNT_ID,
        "trade-oversell-1",
    ),
    AccountingDiagnosticExpectation(
        "ACCOUNTING_NON_NEGATING_REVERSAL",
        INVALID_NON_NEGATING_REVERSAL.event_id,
        BROKERAGE_ACCOUNT_ID,
    ),
)

# Kept as a named fixture reference so the reconciliation has one source for
# both allocations and later accounting implementation tests.
FIFO_ALLOCATION_EXPECTATIONS = PARTIAL_SALE_FIFO_ALLOCATIONS
CORRECTION_AND_REVERSAL = (CORRECTION, REVERSAL)
