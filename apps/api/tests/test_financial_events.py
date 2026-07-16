"""Tests for immutable, Decimal-backed financial-event contracts."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from pia_api.domain.financial_events import (
    CashLeg,
    FinancialEvent,
    FinancialEventBatch,
    FinancialEventType,
    InstrumentLeg,
    Money,
    MovementDirection,
    Quantity,
    SourceIdentity,
    SourceReportedEurEvidence,
)


def _cash(
    direction: MovementDirection, amount: str = "100.00", currency: str = "EUR"
) -> CashLeg:
    return CashLeg(
        direction=direction,
        money=Money(amount=amount, currency=currency),
    )


def _instrument(direction: MovementDirection, quantity: str = "1.25") -> InstrumentLeg:
    return InstrumentLeg(
        direction=direction,
        instrument_id="US0378331005",
        quantity=Quantity(value=quantity),
    )


def _event(**changes: object) -> FinancialEvent:
    values: dict[str, object] = {
        "owner_id": uuid4(),
        "account_id": uuid4(),
        "source_identity": SourceIdentity(
            provider="trade-republic", event_reference="source-event-1"
        ),
        "event_type": FinancialEventType.DEPOSIT,
        "occurred_at": datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        "legs": [_cash(MovementDirection.IN)],
    }
    values.update(changes)
    return FinancialEvent(**values)


def test_money_and_quantity_preserve_decimal_source_precision() -> None:
    money = Money(amount="12.3400", currency="EUR")
    quantity = Quantity(value=Decimal("0.125000"))

    assert money.amount == Decimal("12.3400")
    assert quantity.value == Decimal("0.125000")
    assert _event(
        event_type=FinancialEventType.BUY,
        legs=[
            _cash(MovementDirection.OUT, "12.3400"),
            _instrument(MovementDirection.IN, "0.125000"),
        ],
    ).legs[1].quantity.value == Decimal("0.125000")


@pytest.mark.parametrize(
    ("contract", "field", "value"),
    [
        (Money, "amount", 12.34),
        (Quantity, "value", 0.125),
        (SourceReportedEurEvidence, "source_rate", 0.92),
    ],
)
def test_financial_contracts_reject_python_floats(
    contract: type[Money] | type[Quantity] | type[SourceReportedEurEvidence],
    field: str,
    value: float,
) -> None:
    values: dict[str, object] = {field: value}
    if contract is Money:
        values["currency"] = "EUR"
    elif contract is SourceReportedEurEvidence:
        values.update(
            eur_amount=Money(amount="10.00", currency="EUR"),
            reported_at=datetime(2026, 7, 15, 9, 0, tzinfo=UTC),
        )

    with pytest.raises(ValidationError):
        contract(**values)


@pytest.mark.parametrize("currency", ["eur", "EURO", "EU", "EUR1"])
def test_money_rejects_invalid_currency_codes(currency: str) -> None:
    with pytest.raises(ValidationError):
        Money(amount="1.00", currency=currency)


def test_source_reported_eur_evidence_requires_explicit_eur_facts() -> None:
    evidence = SourceReportedEurEvidence(
        eur_amount=Money(amount="92.00", currency="EUR"),
        source_rate="0.9200",
        reported_at=datetime(2026, 7, 15, 9, 5, tzinfo=UTC),
    )

    assert evidence.eur_amount.amount == Decimal("92.00")
    assert evidence.source_rate == Decimal("0.9200")

    with pytest.raises(ValidationError):
        SourceReportedEurEvidence(
            eur_amount=Money(amount="100.00", currency="USD"),
            source_rate="0.9200",
            reported_at=datetime(2026, 7, 15, 9, 5, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("event_type", "legs", "link_name"),
    [
        (FinancialEventType.DEPOSIT, [_cash(MovementDirection.IN)], None),
        (FinancialEventType.WITHDRAWAL, [_cash(MovementDirection.OUT)], None),
        (
            FinancialEventType.BUY,
            [_cash(MovementDirection.OUT), _instrument(MovementDirection.IN)],
            None,
        ),
        (
            FinancialEventType.SELL,
            [_cash(MovementDirection.IN), _instrument(MovementDirection.OUT)],
            None,
        ),
        (FinancialEventType.DIVIDEND, [_cash(MovementDirection.IN)], None),
        (FinancialEventType.INTEREST, [_cash(MovementDirection.IN)], None),
        (FinancialEventType.FEE, [_cash(MovementDirection.OUT)], None),
        (FinancialEventType.WITHHOLDING_TAX, [_cash(MovementDirection.OUT)], None),
        (
            FinancialEventType.SOURCE_REPORTED_FX_CONVERSION,
            [_cash(MovementDirection.OUT, currency="USD"), _cash(MovementDirection.IN)],
            None,
        ),
        (
            FinancialEventType.STOCK_SPLIT,
            [
                _instrument(MovementDirection.OUT, "1"),
                _instrument(MovementDirection.IN, "2"),
            ],
            None,
        ),
        (
            FinancialEventType.CORRECTION,
            [_cash(MovementDirection.IN)],
            "correction_of_event_id",
        ),
        (
            FinancialEventType.REVERSAL,
            [_cash(MovementDirection.OUT)],
            "reversal_of_event_id",
        ),
    ],
)
def test_each_baseline_event_type_accepts_its_normalized_movement_shape(
    event_type: FinancialEventType,
    legs: list[CashLeg | InstrumentLeg],
    link_name: str | None,
) -> None:
    values: dict[str, object] = {"event_type": event_type, "legs": legs}
    if link_name:
        values[link_name] = uuid4()

    assert _event(**values).event_type is event_type


def test_event_shape_rejects_wrong_legs_and_invalid_correction_links() -> None:
    with pytest.raises(ValidationError):
        _event(
            event_type=FinancialEventType.BUY,
            legs=[_cash(MovementDirection.OUT)],
        )

    with pytest.raises(ValidationError):
        _event(
            event_type=FinancialEventType.CORRECTION,
            legs=[_cash(MovementDirection.IN)],
        )

    with pytest.raises(ValidationError):
        _event(correction_of_event_id=uuid4())

    with pytest.raises(ValidationError):
        _event(
            event_type=FinancialEventType.REVERSAL,
            correction_of_event_id=uuid4(),
            reversal_of_event_id=uuid4(),
        )


def test_event_and_nested_contracts_are_immutable() -> None:
    event = _event()

    with pytest.raises(ValidationError):
        event.event_type = FinancialEventType.WITHDRAWAL
    with pytest.raises(ValidationError):
        event.legs[0].money.amount = Decimal("200.00")


def test_batch_rejects_duplicate_source_identity_for_owner_and_account() -> None:
    owner_id, account_id = uuid4(), uuid4()
    first = _event(owner_id=owner_id, account_id=account_id)
    duplicate = _event(
        owner_id=owner_id,
        account_id=account_id,
        source_identity=first.source_identity,
    )

    with pytest.raises(ValidationError):
        FinancialEventBatch(events=[first, duplicate])

    assert (
        FinancialEventBatch(
            events=[
                first,
                _event(owner_id=owner_id, source_identity=first.source_identity),
            ]
        )
        .events[1]
        .account_id
        != account_id
    )
