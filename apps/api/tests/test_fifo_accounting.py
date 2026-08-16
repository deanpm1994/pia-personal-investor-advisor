"""Regression tests for the P5.6 Decimal FIFO accounting layer."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from accounting_fixtures import (
    ACCOUNT_FIXTURES,
    ACCOUNTING_FIXTURE_HISTORY,
    EXPECTED_FEE_ADJUSTED_PROCEEDS,
    EXPECTED_REALIZED_GAIN,
    FIFO_ALLOCATION_EXPECTATIONS,
    FIFO_REMAINING_LOTS,
    INVALID_OVERSELL,
)
from financial_fixtures import FIRST_BUY, SECOND_BUY

from pia_api.domain.accounting import AccountingAccount, LedgerEvent
from pia_api.domain.fifo_accounting import (
    replay_fifo_accounting,
    serialize_fifo_accounting_result,
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


def _accounts() -> tuple[AccountingAccount, ...]:
    return tuple(
        AccountingAccount(
            account_id=fixture.account_id,
            owner_id=ACCOUNTING_FIXTURE_HISTORY[0].event.owner_id,
        )
        for fixture in ACCOUNT_FIXTURES
    )


def _ledger_events(*fixtures: object) -> tuple[LedgerEvent, ...]:
    return tuple(
        LedgerEvent(
            event_id=fixture.event_id,
            event=fixture.event,
            created_at=fixture.created_at,
            source_group_reference=fixture.source_group_reference,
        )
        for fixture in fixtures
    )


def test_fifo_reconciles_fractional_allocations_lots_fees_and_realized_gain() -> None:
    result = replay_fifo_accounting(
        _accounts(), _ledger_events(*ACCOUNTING_FIXTURE_HISTORY)
    )

    sale = result.realized_sales[0]
    assert tuple(
        (allocation.buy_event_id, allocation.quantity, allocation.allocated_basis)
        for allocation in sale.allocations
    ) == tuple(
        (
            str((FIRST_BUY, SECOND_BUY)[index].event_id),
            expectation.quantity,
            expectation.allocated_basis,
        )
        for index, expectation in enumerate(FIFO_ALLOCATION_EXPECTATIONS)
    )
    assert sale.proceeds == EXPECTED_FEE_ADJUSTED_PROCEEDS
    assert sale.allocated_basis == Decimal("116.150")
    assert sale.realized_gain == EXPECTED_REALIZED_GAIN
    assert sale.source_currency == "EUR"
    assert tuple(
        (lot.buy_event_id, lot.quantity, lot.total_basis) for lot in result.open_lots
    ) == tuple(
        (str(SECOND_BUY.event_id), expectation.quantity, expectation.total_basis)
        for expectation in FIFO_REMAINING_LOTS
    )
    assert result.diagnostics == ()
    assert all(allocation.evidence_event_ids for allocation in sale.allocations)
    assert sale.evidence_event_ids


def test_fifo_is_deterministic_for_shuffled_input() -> None:
    ordered = replay_fifo_accounting(
        _accounts(), _ledger_events(*ACCOUNTING_FIXTURE_HISTORY)
    )
    shuffled = replay_fifo_accounting(
        _accounts(), _ledger_events(*reversed(ACCOUNTING_FIXTURE_HISTORY))
    )

    assert serialize_fifo_accounting_result(
        ordered
    ) == serialize_fifo_accounting_result(shuffled)


def test_withholding_tax_never_changes_lot_basis_proceeds_or_realized_gain() -> None:
    without_tax = tuple(
        fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
        if fixture.event.event_type is not FinancialEventType.WITHHOLDING_TAX
    )

    result = replay_fifo_accounting(_accounts(), _ledger_events(*without_tax))

    assert result.realized_sales[0].proceeds == EXPECTED_FEE_ADJUSTED_PROCEEDS
    assert result.realized_sales[0].realized_gain == EXPECTED_REALIZED_GAIN
    assert result.open_lots[0].total_basis == FIFO_REMAINING_LOTS[0].total_basis


def test_missing_fee_group_is_incomplete_without_guessing_its_base_trade() -> None:
    fee = next(
        fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
        if fixture.event.event_type is FinancialEventType.FEE
    )
    missing_group_fee = replace(fee, source_group_reference=None)
    history = tuple(
        missing_group_fee if fixture == fee else fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
    )

    result = replay_fifo_accounting(_accounts(), _ledger_events(*history))

    assert {diagnostic.code for diagnostic in result.diagnostics} >= {
        "FIFO_MISSING_FEE_ATTRIBUTION"
    }
    assert result.open_lots == ()
    assert result.realized_sales == ()


def test_oversell_withholds_fifo_results_instead_of_producing_partial_gain() -> None:
    result = replay_fifo_accounting(
        _accounts(),
        _ledger_events(*ACCOUNTING_FIXTURE_HISTORY, INVALID_OVERSELL),
    )

    assert {diagnostic.code for diagnostic in result.diagnostics} >= {"FIFO_OVERSELL"}
    assert result.open_lots == ()
    assert result.realized_sales == ()


def test_full_sale_closes_the_remaining_split_lot_with_exact_basis() -> None:
    final_sale = LedgerEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000701"),
        event=FinancialEvent(
            owner_id=FIRST_BUY.event.owner_id,
            account_id=FIRST_BUY.event.account_id,
            source_identity=SourceIdentity(
                provider="synthetic-fixture", event_reference="sell-2"
            ),
            event_type=FinancialEventType.SELL,
            occurred_at=datetime(2026, 7, 17, tzinfo=UTC),
            legs=(
                CashLeg(
                    direction=MovementDirection.IN,
                    money=Money(amount="40.000", currency="EUR"),
                ),
                InstrumentLeg(
                    direction=MovementDirection.OUT,
                    instrument_id=FIRST_BUY.event.legs[1].instrument_id,
                    quantity=Quantity(value="3.500"),
                ),
            ),
        ),
        created_at=datetime(2026, 7, 17, tzinfo=UTC),
    )

    result = replay_fifo_accounting(
        _accounts(), _ledger_events(*ACCOUNTING_FIXTURE_HISTORY) + (final_sale,)
    )

    assert result.open_lots == ()
    assert result.realized_sales[-1].allocated_basis == Decimal("35.350")
    assert result.realized_sales[-1].realized_gain == Decimal("4.650")


def test_ambiguous_group_and_mixed_currency_lots_are_withheld() -> None:
    second_buy = next(
        fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
        if fixture.event.event_type is FinancialEventType.BUY
        and fixture
        != next(
            item
            for item in ACCOUNTING_FIXTURE_HISTORY
            if item.event.event_type is FinancialEventType.BUY
        )
    )
    ambiguous_history = tuple(
        replace(fixture, source_group_reference="trade-buy-1")
        if fixture == second_buy
        else fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
    )
    ambiguous = replay_fifo_accounting(_accounts(), _ledger_events(*ambiguous_history))

    first_buy = next(
        fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
        if fixture.event.event_type is FinancialEventType.BUY
    )
    first_fee = next(
        fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
        if fixture.event.event_type is FinancialEventType.FEE
    )
    usd_buy = replace(
        first_buy,
        event=first_buy.event.model_copy(
            update={
                "legs": (
                    CashLeg(
                        direction=MovementDirection.OUT,
                        money=Money(amount="100.000", currency="USD"),
                    ),
                    first_buy.event.legs[1],
                )
            }
        ),
    )
    usd_fee = replace(
        first_fee,
        event=first_fee.event.model_copy(
            update={
                "legs": (
                    CashLeg(
                        direction=MovementDirection.OUT,
                        money=Money(amount="1.000", currency="USD"),
                    ),
                )
            }
        ),
    )
    mixed_history = tuple(
        usd_buy
        if fixture == first_buy
        else usd_fee
        if fixture == first_fee
        else fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
    )
    mixed = replay_fifo_accounting(_accounts(), _ledger_events(*mixed_history))

    assert {diagnostic.code for diagnostic in ambiguous.diagnostics} >= {
        "FIFO_AMBIGUOUS_FEE_GROUP"
    }
    assert {diagnostic.code for diagnostic in mixed.diagnostics} >= {
        "FIFO_MIXED_CURRENCY_LOTS"
    }
    assert ambiguous.open_lots == mixed.open_lots == ()
    assert ambiguous.realized_sales == mixed.realized_sales == ()


def test_non_reconciling_split_and_instrument_correction_are_incomplete() -> None:
    split = next(
        fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
        if fixture.event.event_type is FinancialEventType.STOCK_SPLIT
    )
    non_reconciling_split = replace(
        split,
        event=split.event.model_copy(
            update={
                "legs": (
                    InstrumentLeg(
                        direction=MovementDirection.OUT,
                        instrument_id=split.event.legs[0].instrument_id,
                        quantity=Quantity(value="1.000"),
                    ),
                    split.event.legs[1],
                )
            }
        ),
    )
    correction = LedgerEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000702"),
        event=FinancialEvent(
            owner_id=FIRST_BUY.event.owner_id,
            account_id=FIRST_BUY.event.account_id,
            source_identity=SourceIdentity(
                provider="synthetic-fixture", event_reference="buy-correction"
            ),
            event_type=FinancialEventType.CORRECTION,
            occurred_at=datetime(2026, 7, 18, tzinfo=UTC),
            correction_of_event_id=FIRST_BUY.event_id,
            legs=FIRST_BUY.event.legs,
        ),
        created_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    history = tuple(
        non_reconciling_split if fixture == split else fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
    )

    result = replay_fifo_accounting(
        _accounts(), _ledger_events(*history) + (correction,)
    )

    assert {diagnostic.code for diagnostic in result.diagnostics} >= {
        "FIFO_NON_RECONCILING_SPLIT",
        "FIFO_UNSUPPORTED_CORRECTION",
    }
    assert result.open_lots == ()
    assert result.realized_sales == ()
