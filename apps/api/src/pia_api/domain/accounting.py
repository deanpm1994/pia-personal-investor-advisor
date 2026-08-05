"""Pure Decimal replay for cash and security quantities.

This module deliberately stops before lots, cost basis, realized gains, snapshots,
or persistence.  It accepts immutable ledger facts and returns a reproducible,
evidence-bearing view of native cash and position quantities.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pia_api.domain.financial_events import (
    CashLeg,
    FinancialEvent,
    FinancialEventType,
    InstrumentLeg,
    MovementDirection,
)


@dataclass(frozen=True)
class AccountingAccount:
    """The owner-scoped account metadata needed for deterministic replay."""

    account_id: UUID
    owner_id: UUID


@dataclass(frozen=True)
class LedgerEvent:
    """Immutable ledger facts plus persistence metadata used by the total order."""

    event_id: UUID
    event: FinancialEvent
    created_at: datetime
    source_group_reference: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, UUID):
            raise ValueError("event_id must be a UUID")
        if not isinstance(self.created_at, datetime):
            raise ValueError("created_at must be a datetime")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone offset")
        if (
            self.source_group_reference is not None
            and not self.source_group_reference.strip()
        ):
            raise ValueError("source_group_reference must not be blank")

    @property
    def group_reference(self) -> str | None:
        """Prefer persisted metadata while allowing the domain contract value."""
        return self.source_group_reference or self.event.source_group_reference


@dataclass(frozen=True)
class AccountingDiagnostic:
    """A stable, evidence-linked reason that a value cannot be safely published."""

    code: str
    event_id: str
    account_id: UUID
    source_group_reference: str | None = None


@dataclass(frozen=True)
class CashBalance:
    """One Decimal native-currency balance with the events that established it."""

    account_id: UUID | None
    currency: str
    amount: Decimal
    evidence_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_decimal(self.amount, "amount")


@dataclass(frozen=True)
class Position:
    """One exact security quantity with its contributing immutable event IDs."""

    account_id: UUID | None
    instrument_id: str
    quantity: Decimal
    evidence_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_decimal(self.quantity, "quantity")


@dataclass(frozen=True)
class AccountingResult:
    """Stable result sets with diagnostics for unavailable values."""

    account_cash_balances: tuple[CashBalance, ...]
    owner_cash_balances: tuple[CashBalance, ...]
    account_positions: tuple[Position, ...]
    owner_positions: tuple[Position, ...]
    diagnostics: tuple[AccountingDiagnostic, ...]


def replay_accounting(
    accounts: Iterable[AccountingAccount], ledger_events: Iterable[LedgerEvent]
) -> AccountingResult:
    """Fold one owner's immutable ledger facts in ADR 0007 replay order.

    Every valid explicit leg contributes exactly once.  When a history becomes
    impossible, the affected account values and owner aggregates are withheld
    instead of being silently repaired or published as partial facts.
    """
    account_by_id = {account.account_id: account for account in accounts}
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
    event_by_id = {entry.event_id: entry for entry in ordered_events}
    order_index = {entry.event_id: index for index, entry in enumerate(ordered_events)}
    duplicate_event_ids = {
        entry.event_id
        for entry in ordered_events
        if sum(other.event_id == entry.event_id for other in ordered_events) > 1
    }
    diagnostics: set[AccountingDiagnostic] = set()
    invalid_accounts: set[UUID] = set()
    cash: dict[tuple[UUID, str], Decimal] = {}
    cash_evidence: dict[tuple[UUID, str], list[str]] = {}
    positions: dict[tuple[UUID, str], Decimal] = {}
    position_evidence: dict[tuple[UUID, str], list[str]] = {}

    def diagnose(entry: LedgerEvent, code: str, *, invalidate: bool = False) -> None:
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
            diagnose(entry, "ACCOUNTING_MISSING_ACCOUNT", invalidate=True)
            continue
        if entry.event_id in duplicate_event_ids:
            diagnose(entry, "ACCOUNTING_DUPLICATE_EVENT_ID", invalidate=True)
        if entry.event.event_type is FinancialEventType.CORRECTION:
            _validate_correction(entry, event_by_id, order_index, diagnose)
        elif entry.event.event_type is FinancialEventType.REVERSAL:
            _validate_reversal(entry, event_by_id, order_index, diagnose)
        if (
            entry.event.event_type is FinancialEventType.FEE
            and entry.group_reference is None
        ):
            diagnose(entry, "ACCOUNTING_MISSING_ATTRIBUTION")

        if entry.event.event_type is FinancialEventType.STOCK_SPLIT:
            _apply_split(entry, positions, position_evidence, diagnose)
            continue

        for leg in entry.event.legs:
            if isinstance(leg, CashLeg):
                key = (entry.event.account_id, leg.money.currency)
                cash[key] = cash.get(key, Decimal("0")) + _signed(
                    leg.direction, leg.money.amount
                )
                cash_evidence.setdefault(key, []).append(str(entry.event_id))
            else:
                _apply_instrument_leg(
                    entry, leg, positions, position_evidence, diagnose
                )

    account_cash_balances = tuple(
        CashBalance(
            account_id=account_id,
            currency=currency,
            amount=amount,
            evidence_event_ids=tuple(cash_evidence[(account_id, currency)]),
        )
        for (account_id, currency), amount in sorted(
            cash.items(), key=lambda item: (str(item[0][0]), item[0][1])
        )
        if account_id not in invalid_accounts
    )
    account_positions = tuple(
        Position(
            account_id=account_id,
            instrument_id=instrument_id,
            quantity=quantity,
            evidence_event_ids=tuple(position_evidence[(account_id, instrument_id)]),
        )
        for (account_id, instrument_id), quantity in sorted(
            positions.items(), key=lambda item: (str(item[0][0]), item[0][1])
        )
        if account_id not in invalid_accounts and quantity != Decimal("0")
    )
    return AccountingResult(
        account_cash_balances=account_cash_balances,
        owner_cash_balances=_aggregate_cash(account_cash_balances, invalid_accounts),
        account_positions=account_positions,
        owner_positions=_aggregate_positions(account_positions, invalid_accounts),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda diagnostic: (
                    diagnostic.code,
                    diagnostic.event_id,
                    diagnostic.account_id,
                    diagnostic.source_group_reference or "",
                ),
            )
        ),
    )


def serialize_accounting_result(result: AccountingResult) -> bytes:
    """Return a canonical byte representation for deterministic-result checks."""
    return json.dumps(
        {
            "account_cash_balances": [
                _cash_data(balance) for balance in result.account_cash_balances
            ],
            "owner_cash_balances": [
                _cash_data(balance) for balance in result.owner_cash_balances
            ],
            "account_positions": [
                _position_data(position) for position in result.account_positions
            ],
            "owner_positions": [
                _position_data(position) for position in result.owner_positions
            ],
            "diagnostics": [
                {
                    "account_id": str(diagnostic.account_id),
                    "code": diagnostic.code,
                    "event_id": diagnostic.event_id,
                    "source_group_reference": diagnostic.source_group_reference,
                }
                for diagnostic in result.diagnostics
            ],
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _validate_correction(
    entry: LedgerEvent,
    event_by_id: dict[UUID, LedgerEvent],
    order_index: dict[UUID, int],
    diagnose: Callable[..., None],
) -> None:
    target = event_by_id.get(entry.event.correction_of_event_id)
    if (
        target is None
        or not _same_scope(entry, target)
        or not _precedes(target, entry, order_index)
        or _has_link_cycle(entry, event_by_id)
    ):
        diagnose(entry, "ACCOUNTING_INVALID_CORRECTION_LINK", invalidate=True)
    elif not _has_compatible_correction_shape(target.event, entry.event):
        diagnose(entry, "ACCOUNTING_UNSUPPORTED_CORRECTION", invalidate=True)


def _validate_reversal(
    entry: LedgerEvent,
    event_by_id: dict[UUID, LedgerEvent],
    order_index: dict[UUID, int],
    diagnose: Callable[..., None],
) -> None:
    target = event_by_id.get(entry.event.reversal_of_event_id)
    if (
        target is None
        or not _same_scope(entry, target)
        or not _precedes(target, entry, order_index)
        or _has_link_cycle(entry, event_by_id)
    ):
        diagnose(entry, "ACCOUNTING_INVALID_REVERSAL_LINK", invalidate=True)
    elif not _negates(target.event, entry.event):
        diagnose(entry, "ACCOUNTING_NON_NEGATING_REVERSAL", invalidate=True)


def _same_scope(left: LedgerEvent, right: LedgerEvent) -> bool:
    return (
        left.event_id != right.event_id
        and left.event.owner_id == right.event.owner_id
        and left.event.account_id == right.event.account_id
    )


def _precedes(
    target: LedgerEvent, entry: LedgerEvent, order_index: dict[UUID, int]
) -> bool:
    return order_index[target.event_id] < order_index[entry.event_id]


def _has_link_cycle(entry: LedgerEvent, event_by_id: dict[UUID, LedgerEvent]) -> bool:
    seen: set[UUID] = set()
    current = entry
    while True:
        linked_event_id = (
            current.event.correction_of_event_id or current.event.reversal_of_event_id
        )
        if linked_event_id is None:
            return False
        if linked_event_id in seen:
            return True
        seen.add(linked_event_id)
        target = event_by_id.get(linked_event_id)
        if target is None:
            return False
        current = target


def _has_compatible_correction_shape(
    target: FinancialEvent, correction: FinancialEvent
) -> bool:
    if len(target.legs) != len(correction.legs):
        return False
    return all(
        type(target_leg) is type(correction_leg)
        and _same_leg_dimension(target_leg, correction_leg)
        for target_leg, correction_leg in zip(target.legs, correction.legs, strict=True)
    )


def _same_leg_dimension(
    left: CashLeg | InstrumentLeg, right: CashLeg | InstrumentLeg
) -> bool:
    if isinstance(left, CashLeg) and isinstance(right, CashLeg):
        return left.money.currency == right.money.currency
    if isinstance(left, InstrumentLeg) and isinstance(right, InstrumentLeg):
        return left.instrument_id == right.instrument_id
    return False


def _negates(target: FinancialEvent, reversal: FinancialEvent) -> bool:
    if len(target.legs) != len(reversal.legs):
        return False
    return all(
        type(target_leg) is type(reversal_leg)
        and target_leg.direction is not reversal_leg.direction
        and _same_leg_fact(target_leg, reversal_leg)
        for target_leg, reversal_leg in zip(target.legs, reversal.legs, strict=True)
    )


def _same_leg_fact(
    left: CashLeg | InstrumentLeg, right: CashLeg | InstrumentLeg
) -> bool:
    if isinstance(left, CashLeg) and isinstance(right, CashLeg):
        return left.money == right.money
    if isinstance(left, InstrumentLeg) and isinstance(right, InstrumentLeg):
        return (
            left.instrument_id == right.instrument_id
            and left.quantity == right.quantity
        )
    return False


def _apply_split(
    entry: LedgerEvent,
    positions: dict[tuple[UUID, str], Decimal],
    position_evidence: dict[tuple[UUID, str], list[str]],
    diagnose: Callable[..., None],
) -> None:
    outbound, inbound = entry.event.legs
    assert isinstance(outbound, InstrumentLeg)
    assert isinstance(inbound, InstrumentLeg)
    if outbound.direction is not MovementDirection.OUT:
        outbound, inbound = inbound, outbound
    key = (entry.event.account_id, outbound.instrument_id)
    prior_quantity = positions.get(key, Decimal("0"))
    if prior_quantity <= Decimal("0"):
        diagnose(entry, "ACCOUNTING_IMPOSSIBLE_SPLIT", invalidate=True)
        return
    positions[key] = prior_quantity / outbound.quantity.value * inbound.quantity.value
    position_evidence.setdefault(key, []).append(str(entry.event_id))


def _apply_instrument_leg(
    entry: LedgerEvent,
    leg: InstrumentLeg,
    positions: dict[tuple[UUID, str], Decimal],
    position_evidence: dict[tuple[UUID, str], list[str]],
    diagnose: Callable[..., None],
) -> None:
    key = (entry.event.account_id, leg.instrument_id)
    next_quantity = positions.get(key, Decimal("0")) + _signed(
        leg.direction, leg.quantity.value
    )
    positions[key] = next_quantity
    position_evidence.setdefault(key, []).append(str(entry.event_id))
    if next_quantity < Decimal("0"):
        diagnose(entry, "ACCOUNTING_OVERSELL", invalidate=True)


def _aggregate_cash(
    balances: tuple[CashBalance, ...], invalid_accounts: set[UUID]
) -> tuple[CashBalance, ...]:
    if invalid_accounts:
        return ()
    totals: dict[str, Decimal] = {}
    evidence: dict[str, list[str]] = {}
    for balance in balances:
        totals[balance.currency] = (
            totals.get(balance.currency, Decimal("0")) + balance.amount
        )
        evidence.setdefault(balance.currency, []).extend(balance.evidence_event_ids)
    return tuple(
        CashBalance(None, currency, amount, tuple(evidence[currency]))
        for currency, amount in sorted(totals.items())
    )


def _aggregate_positions(
    positions: tuple[Position, ...], invalid_accounts: set[UUID]
) -> tuple[Position, ...]:
    if invalid_accounts:
        return ()
    totals: dict[str, Decimal] = {}
    evidence: dict[str, list[str]] = {}
    for position in positions:
        totals[position.instrument_id] = (
            totals.get(position.instrument_id, Decimal("0")) + position.quantity
        )
        evidence.setdefault(position.instrument_id, []).extend(
            position.evidence_event_ids
        )
    return tuple(
        Position(None, instrument_id, quantity, tuple(evidence[instrument_id]))
        for instrument_id, quantity in sorted(totals.items())
    )


def _signed(direction: MovementDirection, value: Decimal) -> Decimal:
    return value if direction is MovementDirection.IN else -value


def _require_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(
            f"{field_name} must be a finite Decimal; floats are not accepted"
        )


def _cash_data(balance: CashBalance) -> dict[str, object]:
    return {
        "account_id": str(balance.account_id) if balance.account_id else None,
        "amount": str(balance.amount),
        "currency": balance.currency,
        "evidence_event_ids": balance.evidence_event_ids,
    }


def _position_data(position: Position) -> dict[str, object]:
    return {
        "account_id": str(position.account_id) if position.account_id else None,
        "evidence_event_ids": position.evidence_event_ids,
        "instrument_id": position.instrument_id,
        "quantity": str(position.quantity),
    }
