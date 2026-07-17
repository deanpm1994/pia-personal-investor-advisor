"""Invariant checks for the hand-worked P3.4 synthetic ledger history."""

from decimal import Decimal

import pytest
from financial_fixtures import (
    CORRECTION,
    DEPOSIT,
    DUPLICATE_DEPOSIT,
    FIRST_BUY,
    FIXTURE_HISTORY,
    PARTIAL_SALE,
    PARTIAL_SALE_FIFO_ALLOCATIONS,
    PARTIAL_SALE_REMAINING_QUANTITY,
    REVERSAL,
    SALE_FEE,
    SOURCE_REPORTED_FX,
    WITHHOLDING_TAX,
    FifoAllocationFixture,
)
from pydantic import ValidationError

from pia_api.domain.financial_events import (
    CashLeg,
    FinancialEventBatch,
    FinancialEventType,
    InstrumentLeg,
    MovementDirection,
)


def test_fixture_history_covers_the_canonical_event_vocabulary() -> None:
    assert {fixture.event.event_type for fixture in FIXTURE_HISTORY} == set(
        FinancialEventType
    )


def test_fixture_history_preserves_declared_decimal_precision() -> None:
    assert DEPOSIT.event.legs[0].money.amount == Decimal("1000.0000")
    assert FIRST_BUY.event.legs[1].quantity.value == Decimal("1.250")
    assert PARTIAL_SALE.event.legs[0].money.amount == Decimal("150.005")
    assert SOURCE_REPORTED_FX.event.source_reported_eur is not None
    assert SOURCE_REPORTED_FX.event.source_reported_eur.source_rate == Decimal("0.9200")


def test_fixture_event_legs_conserve_the_expected_movement_shapes() -> None:
    for fixture in FIXTURE_HISTORY:
        legs = fixture.event.legs
        event_type = fixture.event.event_type
        directions = {leg.direction for leg in legs}
        if event_type in {FinancialEventType.BUY, FinancialEventType.SELL}:
            assert len(legs) == 2
            assert sum(isinstance(leg, CashLeg) for leg in legs) == 1
            assert sum(isinstance(leg, InstrumentLeg) for leg in legs) == 1
            assert directions == {MovementDirection.IN, MovementDirection.OUT}
        elif event_type is FinancialEventType.SOURCE_REPORTED_FX_CONVERSION:
            assert all(isinstance(leg, CashLeg) for leg in legs)
            assert directions == {MovementDirection.IN, MovementDirection.OUT}
            assert len({leg.money.currency for leg in legs}) == 2
        elif event_type is FinancialEventType.STOCK_SPLIT:
            assert all(isinstance(leg, InstrumentLeg) for leg in legs)
            assert directions == {MovementDirection.IN, MovementDirection.OUT}
            assert len({leg.instrument_id for leg in legs}) == 1


def test_fifo_expectation_is_hand_worked_fixture_data_only() -> None:
    assert PARTIAL_SALE_FIFO_ALLOCATIONS == (
        FifoAllocationFixture("buy-1", Decimal("1.250"), Decimal("101.000")),
        FifoAllocationFixture("buy-2", Decimal("0.750"), Decimal("15.150")),
    )
    assert sum(
        allocation.quantity for allocation in PARTIAL_SALE_FIFO_ALLOCATIONS
    ) == Decimal("2.000")
    assert PARTIAL_SALE_REMAINING_QUANTITY == Decimal("1.750")


def test_fees_and_tax_remain_separate_from_trade_and_dividend_facts() -> None:
    assert SALE_FEE.event.event_type is FinancialEventType.FEE
    assert WITHHOLDING_TAX.event.event_type is FinancialEventType.WITHHOLDING_TAX
    assert (
        SALE_FEE.event.source_identity.event_reference
        != PARTIAL_SALE.event.source_identity.event_reference
    )
    assert WITHHOLDING_TAX.event.source_identity.event_reference != "dividend-1"


def test_source_reported_fx_evidence_is_traceable_without_inference() -> None:
    evidence = SOURCE_REPORTED_FX.event.source_reported_eur

    assert evidence is not None
    assert evidence.eur_amount.amount == Decimal("9.2000")
    assert evidence.source_rate == Decimal("0.9200")
    assert evidence.reported_at.isoformat() == "2026-07-16T09:01:00+00:00"


def test_duplicate_source_fact_is_rejected_for_fixture_owner_and_account() -> None:
    with pytest.raises(ValidationError, match="duplicate source identity"):
        FinancialEventBatch(events=[DEPOSIT.event, DUPLICATE_DEPOSIT.event])


def test_corrections_and_reversals_preserve_original_fixture_history() -> None:
    original_amount = DEPOSIT.event.legs[0].money.amount

    assert CORRECTION.event.correction_of_event_id == DEPOSIT.event_id
    assert REVERSAL.event.reversal_of_event_id == DEPOSIT.event_id
    assert DEPOSIT.event.legs[0].money.amount == original_amount
    with pytest.raises(ValidationError):
        DEPOSIT.event.legs[0].money.amount = Decimal("1")
