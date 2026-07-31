"""Invariants for the hand-worked P5.2 accounting fixture oracle."""

from decimal import Decimal

from accounting_fixtures import (
    ACCOUNT_FIXTURES,
    ACCOUNTING_FIXTURE_HISTORY,
    BROKERAGE_ACCOUNT_ID,
    BROKERAGE_ACCOUNTING_HISTORY,
    CASH_ACCOUNT_ID,
    CASH_BALANCE_EXPECTATIONS,
    CORRECTION_AND_REVERSAL,
    EXPECTED_BROKERAGE_TIE_BREAK_ORDER,
    EXPECTED_EUR_OWNER_AGGREGATE,
    EXPECTED_FEE_ADJUSTED_PROCEEDS,
    EXPECTED_OWNER_NATIVE_CASH,
    EXPECTED_REALIZED_GAIN,
    EXPECTED_RESERVE_EUR_BALANCE,
    EXPECTED_RESERVE_EUR_PROGRESS,
    EXPECTED_RESERVE_EUR_TARGET,
    FIFO_ALLOCATION_EXPECTATIONS,
    FIFO_REMAINING_LOTS,
    INCOMPLETE_DIAGNOSTIC_EXPECTATIONS,
    INVALID_NON_NEGATING_REVERSAL,
    INVALID_OVERSELL,
    MANUAL_ACCOUNTING_HISTORY,
    POSITION_EXPECTATIONS,
    RESERVE_EUR_ACCOUNT_ID,
    RESERVE_UNTARGETED_ACCOUNT_ID,
    RESERVE_USD_ACCOUNT_ID,
    SAVINGS_ACCOUNT_ID,
    TRANSFER_IN,
    TRANSFER_OUT,
    AccountRole,
)
from financial_fixtures import CORRECTION, FIRST_BUY, PARTIAL_SALE, REVERSAL

from pia_api.domain.financial_events import CashLeg, MovementDirection


def test_fixture_accounts_cover_every_role_and_optional_reserve_targets() -> None:
    assert {account.role for account in ACCOUNT_FIXTURES} == set(AccountRole)
    targets = {
        account.account_id: account.emergency_reserve_target_eur
        for account in ACCOUNT_FIXTURES
    }
    assert targets[RESERVE_EUR_ACCOUNT_ID] == Decimal("500.0000")
    assert targets[RESERVE_USD_ACCOUNT_ID] == Decimal("250.0000")
    assert targets[CASH_ACCOUNT_ID] is None
    assert targets[SAVINGS_ACCOUNT_ID] is None


def test_manual_opening_balances_are_first_facts_for_their_accounts() -> None:
    manual_events = sorted(
        MANUAL_ACCOUNTING_HISTORY,
        key=lambda fixture: (
            fixture.event.occurred_at,
            fixture.created_at,
            str(fixture.event_id),
        ),
    )

    for account_id in {
        CASH_ACCOUNT_ID,
        SAVINGS_ACCOUNT_ID,
        RESERVE_EUR_ACCOUNT_ID,
        RESERVE_UNTARGETED_ACCOUNT_ID,
        RESERVE_USD_ACCOUNT_ID,
    }:
        account_events = [
            fixture
            for fixture in manual_events
            if fixture.event.account_id == account_id
        ]
        assert account_events[0].is_manual_opening_balance


def test_equal_timestamps_use_created_at_then_uuid_as_the_total_order() -> None:
    ordered = sorted(
        BROKERAGE_ACCOUNTING_HISTORY,
        key=lambda fixture: (
            fixture.event.occurred_at,
            fixture.created_at,
            str(fixture.event_id),
        ),
    )

    assert all(
        fixture.event.occurred_at == BROKERAGE_ACCOUNTING_HISTORY[0].event.occurred_at
        for fixture in BROKERAGE_ACCOUNTING_HISTORY
    )
    assert all(
        fixture.created_at == BROKERAGE_ACCOUNTING_HISTORY[0].created_at
        for fixture in BROKERAGE_ACCOUNTING_HISTORY
    )
    assert (
        tuple(fixture.event.source_identity.event_reference for fixture in ordered)
        == EXPECTED_BROKERAGE_TIE_BREAK_ORDER
    )


def test_grouped_purchase_and_sale_fees_are_explicit_and_taxes_are_not_trade_fees() -> (
    None
):
    groups = {
        fixture.event.source_identity.event_reference: fixture.source_group_reference
        for fixture in BROKERAGE_ACCOUNTING_HISTORY
    }

    assert groups[FIRST_BUY.event.source_identity.event_reference] == "trade-buy-1"
    assert groups["purchase-fee-1"] == "trade-buy-1"
    assert groups["buy-2"] == "trade-buy-2"
    assert groups["purchase-fee-2"] == "trade-buy-2"
    assert groups[PARTIAL_SALE.event.source_identity.event_reference] == "trade-sell-1"
    assert groups["sale-fee-1"] == "trade-sell-1"
    assert groups["withholding-tax-1"] is None


def test_hand_worked_cash_balances_retain_native_currency_precision() -> None:
    assert tuple(
        (balance.account_id, balance.currency, balance.amount)
        for balance in CASH_BALANCE_EXPECTATIONS
    ) == (
        (BROKERAGE_ACCOUNT_ID, "EUR", Decimal("995.2150")),
        (BROKERAGE_ACCOUNT_ID, "USD", Decimal("-10.0000")),
        (CASH_ACCOUNT_ID, "EUR", Decimal("75.0000")),
        (SAVINGS_ACCOUNT_ID, "EUR", Decimal("540.0000")),
        (RESERVE_EUR_ACCOUNT_ID, "EUR", Decimal("350.0000")),
        (RESERVE_UNTARGETED_ACCOUNT_ID, "EUR", Decimal("200.0000")),
        (RESERVE_USD_ACCOUNT_ID, "USD", Decimal("80.0000")),
    )
    assert EXPECTED_OWNER_NATIVE_CASH == {
        "EUR": Decimal("2160.2150"),
        "USD": Decimal("70.0000"),
    }
    assert EXPECTED_EUR_OWNER_AGGREGATE is None
    assert CASH_BALANCE_EXPECTATIONS[0].amount.as_tuple().exponent == -4
    assert CASH_BALANCE_EXPECTATIONS[1].amount.as_tuple().exponent == -4


def test_hand_worked_fifo_sale_split_and_realized_gain_oracle() -> None:
    assert FIFO_ALLOCATION_EXPECTATIONS[0].quantity == Decimal("1.250")
    assert FIFO_ALLOCATION_EXPECTATIONS[0].allocated_basis == Decimal("101.000")
    assert FIFO_ALLOCATION_EXPECTATIONS[1].quantity == Decimal("0.750")
    assert FIFO_ALLOCATION_EXPECTATIONS[1].allocated_basis == Decimal("15.150")
    assert FIFO_REMAINING_LOTS[0].quantity == Decimal("3.500")
    assert FIFO_REMAINING_LOTS[0].total_basis == Decimal("35.350")
    assert POSITION_EXPECTATIONS[0].quantity == Decimal("3.500")
    assert POSITION_EXPECTATIONS[0].remaining_basis == Decimal("35.350")
    assert EXPECTED_FEE_ADJUSTED_PROCEEDS == Decimal("149.010")
    assert EXPECTED_REALIZED_GAIN == Decimal("32.860")


def test_transfer_is_paired_and_leaves_owner_cash_unchanged() -> None:
    outbound_leg = TRANSFER_OUT.event.legs[0]
    inbound_leg = TRANSFER_IN.event.legs[0]

    assert isinstance(outbound_leg, CashLeg)
    assert isinstance(inbound_leg, CashLeg)
    assert TRANSFER_OUT.source_group_reference == TRANSFER_IN.source_group_reference
    assert outbound_leg.direction is MovementDirection.OUT
    assert inbound_leg.direction is MovementDirection.IN
    assert outbound_leg.money == inbound_leg.money


def test_eur_reserve_target_progress_is_exact_and_untargeted_reserve_is_excluded() -> (
    None
):
    assert EXPECTED_RESERVE_EUR_BALANCE == Decimal("350.0000")
    assert EXPECTED_RESERVE_EUR_TARGET == Decimal("500.0000")
    assert EXPECTED_RESERVE_EUR_PROGRESS == Decimal("0.7000")


def test_incomplete_and_impossible_histories_have_explicit_diagnostics() -> None:
    diagnostics = {
        diagnostic.code: diagnostic for diagnostic in INCOMPLETE_DIAGNOSTIC_EXPECTATIONS
    }

    assert (
        diagnostics["ACCOUNTING_NON_EUR_AGGREGATION_UNAVAILABLE"].account_id
        == RESERVE_USD_ACCOUNT_ID
    )
    assert diagnostics["ACCOUNTING_OVERSELL"].event_id == INVALID_OVERSELL.event_id
    assert (
        diagnostics["ACCOUNTING_OVERSELL"].source_group_reference == "trade-oversell-1"
    )
    assert (
        diagnostics["ACCOUNTING_NON_NEGATING_REVERSAL"].event_id
        == INVALID_NON_NEGATING_REVERSAL.event_id
    )


def test_correction_and_reversal_are_explicit_immutable_legs() -> None:
    assert CORRECTION_AND_REVERSAL == (CORRECTION, REVERSAL)
    assert REVERSAL.event.reversal_of_event_id == CORRECTION.event_id
    assert CORRECTION.event.legs[0].money.amount == Decimal("0.0100")
    assert CORRECTION.event.legs[0].direction is MovementDirection.IN
    assert REVERSAL.event.legs[0].money.amount == Decimal("0.0100")
    assert REVERSAL.event.legs[0].direction is MovementDirection.OUT
    assert len(ACCOUNTING_FIXTURE_HISTORY) > len(BROKERAGE_ACCOUNTING_HISTORY)
