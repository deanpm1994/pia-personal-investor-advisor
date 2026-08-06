"""Pure Decimal FIFO lot accounting for supported immutable security trades."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

from pia_api.domain.accounting import (
    AccountingAccount,
    AccountingDiagnostic,
    LedgerEvent,
    replay_accounting,
)
from pia_api.domain.financial_events import (
    CashLeg,
    FinancialEventType,
    InstrumentLeg,
    MovementDirection,
)


@dataclass(frozen=True)
class OpenLot:
    """The exact remaining basis of one FIFO acquisition lot."""

    account_id: UUID
    instrument_id: str
    source_currency: str
    buy_event_id: str
    quantity: Decimal
    total_basis: Decimal
    fee_event_ids: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_decimal(self.quantity, "quantity")
        _require_decimal(self.total_basis, "total_basis")


@dataclass(frozen=True)
class FifoAllocation:
    """One sale's exact allocation from an acquisition lot."""

    sale_event_id: str
    buy_event_id: str
    account_id: UUID
    instrument_id: str
    source_currency: str
    quantity: Decimal
    allocated_basis: Decimal
    evidence_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_decimal(self.quantity, "quantity")
        _require_decimal(self.allocated_basis, "allocated_basis")


@dataclass(frozen=True)
class RealizedSale:
    """A fee-adjusted sale result with all contributing immutable evidence."""

    sale_event_id: str
    account_id: UUID
    instrument_id: str
    source_currency: str
    quantity: Decimal
    proceeds: Decimal
    allocated_basis: Decimal
    realized_gain: Decimal
    allocations: tuple[FifoAllocation, ...]
    evidence_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name in ("quantity", "proceeds", "allocated_basis", "realized_gain"):
            _require_decimal(getattr(self, field_name), field_name)


@dataclass(frozen=True)
class FifoAccountingResult:
    """Stable FIFO lots and realized sales, or diagnostics when unavailable."""

    open_lots: tuple[OpenLot, ...]
    realized_sales: tuple[RealizedSale, ...]
    diagnostics: tuple[AccountingDiagnostic, ...]


@dataclass
class _WorkingLot:
    account_id: UUID
    instrument_id: str
    source_currency: str
    buy_event_id: str
    quantity: Decimal
    total_basis: Decimal
    fee_event_ids: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]


def replay_fifo_accounting(
    accounts: Iterable[AccountingAccount], ledger_events: Iterable[LedgerEvent]
) -> FifoAccountingResult:
    """Replay supported security trades into FIFO lots without inferring facts.

    The underlying P5.5 fold supplies the shared total order and ledger-level
    consistency diagnostics.  This layer then applies only the explicitly
    approved FIFO, fee, tax, and split policy from ADR 0007.
    """
    accounts = tuple(accounts)
    ordered_events = tuple(
        sorted(
            ledger_events,
            key=lambda entry: (
                entry.event.occurred_at,
                entry.created_at,
                str(entry.event_id),
            ),
        )
    )
    account_by_id = {account.account_id: account for account in accounts}
    base_result = replay_accounting(accounts, ordered_events)
    diagnostics: set[AccountingDiagnostic] = set(base_result.diagnostics)
    invalid_accounts = {
        diagnostic.account_id
        for diagnostic in base_result.diagnostics
        if diagnostic.code != "ACCOUNTING_MISSING_ATTRIBUTION"
    }

    def diagnose(entry: LedgerEvent, code: str, *, invalidate: bool = True) -> None:
        diagnostics.add(
            AccountingDiagnostic(
                code=code,
                event_id=str(entry.event_id),
                account_id=entry.event.account_id,
                source_group_reference=entry.group_reference,
            )
        )
        if invalidate:
            invalid_accounts.add(entry.event.account_id)

    for entry in ordered_events:
        account = account_by_id.get(entry.event.account_id)
        if account is None or account.owner_id != entry.event.owner_id:
            diagnose(entry, "FIFO_MISSING_ACCOUNT")

    fees_by_trade = _group_fees(ordered_events, diagnose)
    lots: list[_WorkingLot] = []
    realized_sales: list[RealizedSale] = []

    for entry in ordered_events:
        event_type = entry.event.event_type
        if event_type is FinancialEventType.BUY:
            _create_lot(entry, fees_by_trade.get(entry.event_id, ()), lots, diagnose)
        elif event_type is FinancialEventType.SELL:
            sale = _allocate_sale(
                entry, fees_by_trade.get(entry.event_id, ()), lots, diagnose
            )
            if sale is not None:
                realized_sales.append(sale)
        elif event_type is FinancialEventType.STOCK_SPLIT:
            _apply_split(entry, lots, diagnose)
        elif event_type in {
            FinancialEventType.CORRECTION,
            FinancialEventType.REVERSAL,
        } and any(isinstance(leg, InstrumentLeg) for leg in entry.event.legs):
            diagnose(entry, "FIFO_UNSUPPORTED_CORRECTION")

    open_lots = tuple(
        OpenLot(
            account_id=lot.account_id,
            instrument_id=lot.instrument_id,
            source_currency=lot.source_currency,
            buy_event_id=lot.buy_event_id,
            quantity=lot.quantity,
            total_basis=lot.total_basis,
            fee_event_ids=lot.fee_event_ids,
            evidence_event_ids=lot.evidence_event_ids,
        )
        for lot in lots
        if lot.quantity > Decimal("0") and lot.account_id not in invalid_accounts
    )
    return FifoAccountingResult(
        open_lots=tuple(
            sorted(
                open_lots,
                key=lambda lot: (
                    str(lot.account_id),
                    lot.instrument_id,
                    lot.buy_event_id,
                ),
            )
        ),
        realized_sales=tuple(
            sale for sale in realized_sales if sale.account_id not in invalid_accounts
        ),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda diagnostic: (
                    diagnostic.code,
                    diagnostic.event_id,
                    str(diagnostic.account_id),
                    diagnostic.source_group_reference or "",
                ),
            )
        ),
    )


def serialize_fifo_accounting_result(result: FifoAccountingResult) -> bytes:
    """Return a canonical byte representation for deterministic-result checks."""
    return json.dumps(
        {
            "diagnostics": [
                _diagnostic_data(diagnostic) for diagnostic in result.diagnostics
            ],
            "open_lots": [_lot_data(lot) for lot in result.open_lots],
            "realized_sales": [_sale_data(sale) for sale in result.realized_sales],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _group_fees(
    ordered_events: tuple[LedgerEvent, ...],
    diagnose: Callable[[LedgerEvent, str], None],
) -> dict[UUID, tuple[LedgerEvent, ...]]:
    trades_by_group: dict[tuple[UUID, UUID, str, str, str], list[LedgerEvent]] = (
        defaultdict(list)
    )
    for entry in ordered_events:
        if entry.event.event_type not in {
            FinancialEventType.BUY,
            FinancialEventType.SELL,
        }:
            continue
        group_reference = entry.group_reference
        if group_reference is None:
            continue
        cash_leg = _trade_cash_leg(entry)
        if cash_leg is not None:
            trades_by_group[
                (
                    entry.event.owner_id,
                    entry.event.account_id,
                    entry.event.source_identity.provider,
                    group_reference,
                    cash_leg.money.currency,
                )
            ].append(entry)

    grouped: dict[UUID, list[LedgerEvent]] = defaultdict(list)
    for fee in ordered_events:
        if fee.event.event_type is not FinancialEventType.FEE:
            continue
        group_reference = fee.group_reference
        if group_reference is None:
            diagnose(fee, "FIFO_MISSING_FEE_ATTRIBUTION")
            continue
        fee_leg = _fee_cash_leg(fee)
        if fee_leg is None:
            diagnose(fee, "FIFO_UNSUPPORTED_FEE")
            continue
        candidates = trades_by_group.get(
            (
                fee.event.owner_id,
                fee.event.account_id,
                fee.event.source_identity.provider,
                group_reference,
                fee_leg.money.currency,
            ),
            [],
        )
        if len(candidates) == 1:
            grouped[candidates[0].event_id].append(fee)
        elif len(candidates) == 0:
            diagnose(fee, "FIFO_UNATTRIBUTED_FEE_GROUP")
        else:
            diagnose(fee, "FIFO_AMBIGUOUS_FEE_GROUP")
    return {event_id: tuple(fees) for event_id, fees in grouped.items()}


def _create_lot(
    entry: LedgerEvent,
    fees: tuple[LedgerEvent, ...],
    lots: list[_WorkingLot],
    diagnose: Callable[[LedgerEvent, str], None],
) -> None:
    cash_leg = _trade_cash_leg(entry)
    instrument_leg = _trade_instrument_leg(entry)
    if cash_leg is None or instrument_leg is None:
        diagnose(entry, "FIFO_UNSUPPORTED_TRADE")
        return
    fee_amount = sum((_fee_cash_leg(fee).money.amount for fee in fees), Decimal("0"))
    fee_event_ids = tuple(str(fee.event_id) for fee in fees)
    lots.append(
        _WorkingLot(
            account_id=entry.event.account_id,
            instrument_id=instrument_leg.instrument_id,
            source_currency=cash_leg.money.currency,
            buy_event_id=str(entry.event_id),
            quantity=instrument_leg.quantity.value,
            total_basis=cash_leg.money.amount + fee_amount,
            fee_event_ids=fee_event_ids,
            evidence_event_ids=(str(entry.event_id), *fee_event_ids),
        )
    )


def _allocate_sale(
    entry: LedgerEvent,
    fees: tuple[LedgerEvent, ...],
    lots: list[_WorkingLot],
    diagnose: Callable[[LedgerEvent, str], None],
) -> RealizedSale | None:
    cash_leg = _trade_cash_leg(entry)
    instrument_leg = _trade_instrument_leg(entry)
    if cash_leg is None or instrument_leg is None:
        diagnose(entry, "FIFO_UNSUPPORTED_TRADE")
        return None
    matching_lots = [
        lot
        for lot in lots
        if lot.account_id == entry.event.account_id
        and lot.instrument_id == instrument_leg.instrument_id
        and lot.quantity > Decimal("0")
    ]
    if any(lot.source_currency != cash_leg.money.currency for lot in matching_lots):
        diagnose(entry, "FIFO_MIXED_CURRENCY_LOTS")
        return None
    available_quantity = sum((lot.quantity for lot in matching_lots), Decimal("0"))
    if available_quantity < instrument_leg.quantity.value:
        diagnose(entry, "FIFO_OVERSELL")
        return None

    remaining = instrument_leg.quantity.value
    planned: list[tuple[_WorkingLot, Decimal, Decimal]] = []
    for lot in matching_lots:
        if remaining == Decimal("0"):
            break
        quantity = min(lot.quantity, remaining)
        basis = (
            lot.total_basis
            if quantity == lot.quantity
            else _proportional(lot.total_basis, quantity, lot.quantity)
        )
        if basis is None:
            diagnose(entry, "FIFO_UNREPRESENTABLE_PRECISION")
            return None
        planned.append((lot, quantity, basis))
        remaining -= quantity

    sale_fee_ids = tuple(str(fee.event_id) for fee in fees)
    proceeds = cash_leg.money.amount - sum(
        (_fee_cash_leg(fee).money.amount for fee in fees), Decimal("0")
    )
    allocations = tuple(
        FifoAllocation(
            sale_event_id=str(entry.event_id),
            buy_event_id=lot.buy_event_id,
            account_id=entry.event.account_id,
            instrument_id=instrument_leg.instrument_id,
            source_currency=cash_leg.money.currency,
            quantity=quantity,
            allocated_basis=basis,
            evidence_event_ids=(
                *lot.evidence_event_ids,
                str(entry.event_id),
                *sale_fee_ids,
            ),
        )
        for lot, quantity, basis in planned
    )
    for lot, quantity, basis in planned:
        lot.quantity -= quantity
        lot.total_basis -= basis
    allocated_basis = sum(
        (allocation.allocated_basis for allocation in allocations), Decimal("0")
    )
    return RealizedSale(
        sale_event_id=str(entry.event_id),
        account_id=entry.event.account_id,
        instrument_id=instrument_leg.instrument_id,
        source_currency=cash_leg.money.currency,
        quantity=instrument_leg.quantity.value,
        proceeds=proceeds,
        allocated_basis=allocated_basis,
        realized_gain=proceeds - allocated_basis,
        allocations=allocations,
        evidence_event_ids=tuple(
            dict.fromkeys(
                (
                    str(entry.event_id),
                    *sale_fee_ids,
                    *(
                        event_id
                        for allocation in allocations
                        for event_id in allocation.evidence_event_ids
                    ),
                )
            )
        ),
    )


def _apply_split(
    entry: LedgerEvent,
    lots: list[_WorkingLot],
    diagnose: Callable[[LedgerEvent, str], None],
) -> None:
    outbound, inbound = entry.event.legs
    assert isinstance(outbound, InstrumentLeg)
    assert isinstance(inbound, InstrumentLeg)
    if outbound.direction is not MovementDirection.OUT:
        outbound, inbound = inbound, outbound
    affected_lots = [
        lot
        for lot in lots
        if lot.account_id == entry.event.account_id
        and lot.instrument_id == outbound.instrument_id
        and lot.quantity > Decimal("0")
    ]
    if not affected_lots:
        diagnose(entry, "FIFO_IMPOSSIBLE_SPLIT")
        return
    if (
        sum((lot.quantity for lot in affected_lots), Decimal("0"))
        != outbound.quantity.value
    ):
        diagnose(entry, "FIFO_NON_RECONCILING_SPLIT")
        return
    transformed_quantities = [
        _proportional(lot.quantity, inbound.quantity.value, outbound.quantity.value)
        for lot in affected_lots
    ]
    if any(
        quantity is None or quantity <= Decimal("0")
        for quantity in transformed_quantities
    ):
        diagnose(entry, "FIFO_UNREPRESENTABLE_SPLIT")
        return
    for lot, quantity in zip(affected_lots, transformed_quantities, strict=True):
        assert quantity is not None
        lot.quantity = quantity
        lot.evidence_event_ids = (*lot.evidence_event_ids, str(entry.event_id))


def _trade_cash_leg(entry: LedgerEvent) -> CashLeg | None:
    return next((leg for leg in entry.event.legs if isinstance(leg, CashLeg)), None)


def _trade_instrument_leg(entry: LedgerEvent) -> InstrumentLeg | None:
    return next(
        (leg for leg in entry.event.legs if isinstance(leg, InstrumentLeg)), None
    )


def _fee_cash_leg(entry: LedgerEvent) -> CashLeg | None:
    return entry.event.legs[0] if isinstance(entry.event.legs[0], CashLeg) else None


def _proportional(
    total: Decimal, numerator: Decimal, denominator: Decimal
) -> Decimal | None:
    """Return an exact finite-Decimal proportion, never a context-rounded value."""
    fraction = Fraction(total) * Fraction(numerator) / Fraction(denominator)
    divisor = fraction.denominator
    power_of_two = 0
    power_of_five = 0
    while divisor % 2 == 0:
        divisor //= 2
        power_of_two += 1
    while divisor % 5 == 0:
        divisor //= 5
        power_of_five += 1
    if divisor != 1:
        return None
    scale = max(power_of_two, power_of_five)
    return Decimal(fraction.numerator * 10**scale // fraction.denominator).scaleb(
        -scale
    )


def _require_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(
            f"{field_name} must be a finite Decimal; floats are not accepted"
        )


def _diagnostic_data(diagnostic: AccountingDiagnostic) -> dict[str, object]:
    return {
        "account_id": str(diagnostic.account_id),
        "code": diagnostic.code,
        "event_id": diagnostic.event_id,
        "source_group_reference": diagnostic.source_group_reference,
    }


def _lot_data(lot: OpenLot) -> dict[str, object]:
    return {
        "account_id": str(lot.account_id),
        "buy_event_id": lot.buy_event_id,
        "evidence_event_ids": lot.evidence_event_ids,
        "fee_event_ids": lot.fee_event_ids,
        "instrument_id": lot.instrument_id,
        "quantity": str(lot.quantity),
        "source_currency": lot.source_currency,
        "total_basis": str(lot.total_basis),
    }


def _sale_data(sale: RealizedSale) -> dict[str, object]:
    return {
        "account_id": str(sale.account_id),
        "allocations": [
            {
                "allocated_basis": str(allocation.allocated_basis),
                "buy_event_id": allocation.buy_event_id,
                "evidence_event_ids": allocation.evidence_event_ids,
                "quantity": str(allocation.quantity),
                "sale_event_id": allocation.sale_event_id,
            }
            for allocation in sale.allocations
        ],
        "allocated_basis": str(sale.allocated_basis),
        "evidence_event_ids": sale.evidence_event_ids,
        "instrument_id": sale.instrument_id,
        "proceeds": str(sale.proceeds),
        "quantity": str(sale.quantity),
        "realized_gain": str(sale.realized_gain),
        "sale_event_id": sale.sale_event_id,
        "source_currency": sale.source_currency,
    }
