"""Regression tests for the P5.5 deterministic cash and position fold."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from accounting_fixtures import (
    ACCOUNT_FIXTURES,
    ACCOUNTING_FIXTURE_HISTORY,
    BROKERAGE_ACCOUNT_ID,
    CASH_BALANCE_EXPECTATIONS,
    EXPECTED_OWNER_NATIVE_CASH,
    INCOMPLETE_DIAGNOSTIC_EXPECTATIONS,
    INVALID_NON_NEGATING_REVERSAL,
    INVALID_OVERSELL,
    POSITION_EXPECTATIONS,
)

from pia_api.domain.accounting import (
    AccountingAccount,
    CashBalance,
    LedgerEvent,
    replay_accounting,
    serialize_accounting_result,
)
from pia_api.domain.financial_events import (
    CashLeg,
    FinancialEvent,
    FinancialEventType,
    Money,
    MovementDirection,
    SourceIdentity,
)


def _accounts() -> tuple[AccountingAccount, ...]:
    return tuple(
        AccountingAccount(
            account_id=fixture.account_id,
            owner_id=next(event.event.owner_id for event in ACCOUNTING_FIXTURE_HISTORY),
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


def _result_for_fixture_history():
    return replay_accounting(_accounts(), _ledger_events(*ACCOUNTING_FIXTURE_HISTORY))


def test_fold_reconciles_exact_cash_positions_and_evidence() -> None:
    result = _result_for_fixture_history()

    assert tuple(
        (balance.account_id, balance.currency, balance.amount)
        for balance in result.account_cash_balances
    ) == tuple(
        (expected.account_id, expected.currency, expected.amount)
        for expected in CASH_BALANCE_EXPECTATIONS
    )
    assert tuple(
        (balance.currency, balance.amount) for balance in result.owner_cash_balances
    ) == tuple(EXPECTED_OWNER_NATIVE_CASH.items())
    assert tuple(
        (position.account_id, position.instrument_id, position.quantity)
        for position in result.account_positions
    ) == tuple(
        (expected.account_id, expected.instrument_id, expected.quantity)
        for expected in POSITION_EXPECTATIONS
    )
    assert tuple(
        (position.instrument_id, position.quantity)
        for position in result.owner_positions
    ) == tuple(
        (expected.instrument_id, expected.quantity)
        for expected in POSITION_EXPECTATIONS
    )
    assert result.diagnostics == ()
    assert all(balance.evidence_event_ids for balance in result.account_cash_balances)
    assert result.account_positions[0].evidence_event_ids[-1].endswith("313")


def test_replay_order_makes_serialized_results_byte_equivalent() -> None:
    ordered = _result_for_fixture_history()
    shuffled = replay_accounting(
        _accounts(), _ledger_events(*reversed(ACCOUNTING_FIXTURE_HISTORY))
    )

    assert serialize_accounting_result(ordered) == serialize_accounting_result(shuffled)


def test_replay_is_invariant_to_each_fixture_history_rotation() -> None:
    baseline = serialize_accounting_result(_result_for_fixture_history())

    for offset in range(len(ACCOUNTING_FIXTURE_HISTORY)):
        rotated = (
            ACCOUNTING_FIXTURE_HISTORY[offset:] + ACCOUNTING_FIXTURE_HISTORY[:offset]
        )
        result = replay_accounting(_accounts(), _ledger_events(*rotated))
        assert serialize_accounting_result(result) == baseline


def test_impossible_histories_are_diagnostic_and_do_not_publish_aggregates() -> None:
    base = _ledger_events(*ACCOUNTING_FIXTURE_HISTORY)
    result = replay_accounting(
        _accounts(),
        base + _ledger_events(INVALID_OVERSELL, INVALID_NON_NEGATING_REVERSAL),
    )

    expected = {
        (diagnostic.code, str(diagnostic.event_id), diagnostic.account_id)
        for diagnostic in INCOMPLETE_DIAGNOSTIC_EXPECTATIONS
        if diagnostic.code != "ACCOUNTING_NON_EUR_AGGREGATION_UNAVAILABLE"
    }
    assert {
        (diagnostic.code, diagnostic.event_id, diagnostic.account_id)
        for diagnostic in result.diagnostics
    } == expected
    assert not any(
        position.account_id == BROKERAGE_ACCOUNT_ID
        for position in result.account_positions
    )
    assert result.owner_positions == ()


def test_unattributed_fee_is_visible_without_discarding_its_cash_fact() -> None:
    fee = next(
        fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
        if fixture.event.event_type is FinancialEventType.FEE
    )
    unattributed_fee = replace(fee, source_group_reference=None)

    result = replay_accounting(
        _accounts(),
        _ledger_events(
            *(fixture for fixture in ACCOUNTING_FIXTURE_HISTORY if fixture != fee),
            unattributed_fee,
        ),
    )

    assert result.diagnostics[-1].code == "ACCOUNTING_MISSING_ATTRIBUTION"
    assert result.diagnostics[-1].event_id == str(unattributed_fee.event_id)


def test_split_without_an_open_position_is_incomplete() -> None:
    split = next(
        fixture
        for fixture in ACCOUNTING_FIXTURE_HISTORY
        if fixture.event.event_type is FinancialEventType.STOCK_SPLIT
    )

    result = replay_accounting(_accounts(), _ledger_events(split))

    assert result.diagnostics[0].code == "ACCOUNTING_IMPOSSIBLE_SPLIT"
    assert result.account_positions == ()


def test_contract_rejects_float_timestamps_and_money_inputs() -> None:
    with pytest.raises(ValueError, match="must be a datetime"):
        LedgerEvent(
            event_id=INVALID_OVERSELL.event_id,
            event=INVALID_OVERSELL.event,
            created_at=1.0,
        )

    with pytest.raises(ValueError, match="floats are not accepted"):
        Money(amount=1.0, currency="EUR")

    with pytest.raises(ValueError, match="floats are not accepted"):
        CashBalance(None, "EUR", 1.0, ())


def test_reversal_with_different_owner_or_account_is_incomplete() -> None:
    target = _ledger_events(*ACCOUNTING_FIXTURE_HISTORY)[0]
    other_account_id = next(
        account.account_id
        for account in _accounts()
        if account.account_id != target.event.account_id
    )
    mismatched_event = FinancialEvent(
        owner_id=target.event.owner_id,
        account_id=other_account_id,
        source_identity=SourceIdentity(
            provider="test", event_reference="wrong-scope-reversal"
        ),
        event_type=FinancialEventType.REVERSAL,
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        reversal_of_event_id=target.event_id,
        legs=[
            CashLeg(
                direction=MovementDirection.OUT,
                money=Money(amount="1000.0000", currency="EUR"),
            )
        ],
    )

    result = replay_accounting(
        _accounts(),
        (target,)
        + (
            LedgerEvent(
                event_id=INVALID_NON_NEGATING_REVERSAL.event_id,
                event=mismatched_event,
                created_at=datetime(2026, 8, 1, tzinfo=UTC),
            ),
        ),
    )

    assert result.diagnostics[-1].code == "ACCOUNTING_INVALID_REVERSAL_LINK"


def test_duplicate_immutable_event_ids_are_incomplete() -> None:
    ledger_events = _ledger_events(*ACCOUNTING_FIXTURE_HISTORY)

    result = replay_accounting(_accounts(), ledger_events + (ledger_events[0],))

    assert result.diagnostics[0].code == "ACCOUNTING_DUPLICATE_EVENT_ID"
    assert result.owner_cash_balances == ()
