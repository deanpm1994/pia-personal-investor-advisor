"""Canonical, evidence-bearing material for immutable financial snapshots.

This module has no database or HTTP dependency.  It turns one atomically read
owner ledger into the deterministic payload that snapshot persistence records.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from pia_api.domain.accounting import (
    AccountingAccount,
    AccountingDiagnostic,
    LedgerEvent,
    replay_accounting,
)
from pia_api.domain.fifo_accounting import replay_fifo_accounting
from pia_api.domain.financial_events import CashLeg, InstrumentLeg

SNAPSHOT_ACCOUNTING_POLICY_VERSION = "adr-0007-v1"
SNAPSHOT_SERIALIZATION_VERSION = "financial-snapshot-v1"


@dataclass(frozen=True)
class SnapshotAccount:
    """Owner-scoped account metadata captured by a snapshot refresh."""

    account_id: UUID
    owner_id: UUID
    name: str
    role: str
    archived_at: datetime | None
    emergency_reserve_target_eur: Decimal | None
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("snapshot account name must not be blank")
        if self.role not in {"brokerage", "cash", "savings", "emergency_reserve"}:
            raise ValueError("snapshot account role is unsupported")
        for field_name in ("archived_at", "updated_at"):
            value = getattr(self, field_name)
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"{field_name} must include a timezone offset")
        if self.emergency_reserve_target_eur is not None:
            _require_decimal(self.emergency_reserve_target_eur, "target")
            if self.emergency_reserve_target_eur <= 0:
                raise ValueError("target must be greater than zero")
        if self.role != "emergency_reserve" and self.emergency_reserve_target_eur:
            raise ValueError("only emergency reserves may have a target")


@dataclass(frozen=True)
class SnapshotMaterial:
    """The complete deterministic content inserted by one successful refresh."""

    fingerprint: str
    as_of: datetime | None
    input_watermark: dict[str, object]
    input_counts: dict[str, int]
    content: dict[str, object]


def build_snapshot_material(
    accounts: Iterable[SnapshotAccount], ledger_events: Iterable[LedgerEvent]
) -> SnapshotMaterial:
    """Build one immutable snapshot payload from immutable facts.

    The caller is responsible for reading both input relations inside one
    transaction.  This function deliberately accepts no current time or client
    display values, so unchanged inputs always yield the same fingerprint.
    """
    ordered_accounts = tuple(
        sorted(accounts, key=lambda account: str(account.account_id))
    )
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
    _validate_owner_scope(ordered_accounts, ordered_events)
    accounting_accounts = tuple(
        AccountingAccount(account_id=account.account_id, owner_id=account.owner_id)
        for account in ordered_accounts
    )
    accounting = replay_accounting(accounting_accounts, ordered_events)
    fifo = replay_fifo_accounting(accounting_accounts, ordered_events)
    input_counts = {
        "accounts": len(ordered_accounts),
        "events": len(ordered_events),
        "legs": sum(len(entry.event.legs) for entry in ordered_events),
    }
    watermark = _input_watermark(ordered_accounts, ordered_events)
    diagnostics, reserve_progress = _diagnostics_and_reserve_progress(
        ordered_accounts, accounting, fifo
    )
    content = {
        "account_summaries": [_account_data(account) for account in ordered_accounts],
        "as_of": _timestamp(ordered_events[-1].event.occurred_at)
        if ordered_events
        else None,
        "cash_by_currency": {
            "accounts": [
                _cash_data(balance) for balance in accounting.account_cash_balances
            ],
            "owner": {
                balance.currency: _decimal(balance.amount)
                for balance in accounting.owner_cash_balances
            },
        },
        "diagnostics": diagnostics,
        "evidence_event_ids": [str(entry.event_id) for entry in ordered_events],
        "fifo": {
            "open_lots": [_lot_data(lot) for lot in fifo.open_lots],
            "realized_sales": [_sale_data(sale) for sale in fifo.realized_sales],
        },
        "positions": {
            "accounts": [
                _position_data(position) for position in accounting.account_positions
            ],
            "owner": [
                _position_data(position) for position in accounting.owner_positions
            ],
        },
        "reserve_progress": reserve_progress,
    }
    fingerprint_input = {
        "accounts": [
            _fingerprint_account_data(account) for account in ordered_accounts
        ],
        "inclusion_boundary": {"kind": "all-owner-ledger-facts"},
        "ledger_events": [_fingerprint_event_data(entry) for entry in ordered_events],
        "owner_id": str(ordered_accounts[0].owner_id) if ordered_accounts else None,
        "policy_version": SNAPSHOT_ACCOUNTING_POLICY_VERSION,
        "serialization_version": SNAPSHOT_SERIALIZATION_VERSION,
    }
    fingerprint = hashlib.sha256(_canonical_json(fingerprint_input)).hexdigest()
    as_of = ordered_events[-1].event.occurred_at if ordered_events else None
    return SnapshotMaterial(
        fingerprint=fingerprint,
        as_of=as_of,
        input_watermark=watermark,
        input_counts=input_counts,
        content=content,
    )


def _validate_owner_scope(
    accounts: tuple[SnapshotAccount, ...], events: tuple[LedgerEvent, ...]
) -> None:
    owner_ids = {account.owner_id for account in accounts}
    owner_ids.update(entry.event.owner_id for entry in events)
    if len(owner_ids) > 1:
        raise ValueError("a snapshot may contain one owner only")
    account_ids = {account.account_id for account in accounts}
    if any(entry.event.account_id not in account_ids for entry in events):
        raise ValueError("snapshot ledger event references an unknown account")


def _input_watermark(
    accounts: tuple[SnapshotAccount, ...], events: tuple[LedgerEvent, ...]
) -> dict[str, object]:
    latest_event = max(
        events,
        key=lambda entry: (entry.created_at, str(entry.event_id)),
        default=None,
    )
    latest_account = max(
        accounts,
        key=lambda account: (account.updated_at, str(account.account_id)),
        default=None,
    )
    return {
        "latest_account_id": str(latest_account.account_id) if latest_account else None,
        "latest_account_updated_at": _timestamp(latest_account.updated_at)
        if latest_account
        else None,
        "latest_event_created_at": _timestamp(latest_event.created_at)
        if latest_event
        else None,
        "latest_event_id": str(latest_event.event_id) if latest_event else None,
    }


def _diagnostics_and_reserve_progress(
    accounts: tuple[SnapshotAccount, ...], accounting: object, fifo: object
) -> tuple[list[dict[str, object]], dict[str, object]]:
    # Both accounting result types use the same stable AccountingDiagnostic shape.
    diagnostic_data = {
        _canonical_json(_diagnostic_data(diagnostic)): _diagnostic_data(diagnostic)
        for diagnostic in (*accounting.diagnostics, *fifo.diagnostics)
    }
    balances_by_account: dict[UUID, list[object]] = {}
    for balance in accounting.account_cash_balances:
        balances_by_account.setdefault(balance.account_id, []).append(balance)
    target_accounts = [
        account
        for account in accounts
        if account.role == "emergency_reserve"
        and account.emergency_reserve_target_eur is not None
    ]
    if not target_accounts:
        return (
            [diagnostic_data[key] for key in sorted(diagnostic_data)],
            {
                "available_eur_balance": None,
                "configured_target_eur": None,
                "status": "unavailable",
            },
        )
    available_eur_balance = Decimal("0")
    target_total = sum(
        (account.emergency_reserve_target_eur for account in target_accounts),
        Decimal("0"),
    )
    incomplete = False
    for account in target_accounts:
        balances = balances_by_account.get(account.account_id, [])
        for balance in balances:
            if balance.currency == "EUR":
                available_eur_balance += balance.amount
            elif balance.amount != Decimal("0"):
                incomplete = True
                diagnostic = {
                    "account_id": str(account.account_id),
                    "code": "SNAPSHOT_RESERVE_NON_EUR_BALANCE",
                    "evidence_event_ids": list(balance.evidence_event_ids),
                    "source_group_reference": None,
                }
                diagnostic_data[_canonical_json(diagnostic)] = diagnostic
    invalid_account_ids = {
        diagnostic.account_id for diagnostic in accounting.diagnostics
    }
    if any(account.account_id in invalid_account_ids for account in target_accounts):
        incomplete = True
    return (
        [diagnostic_data[key] for key in sorted(diagnostic_data)],
        {
            "available_eur_balance": _decimal(available_eur_balance),
            "configured_target_eur": _decimal(target_total),
            "status": "incomplete" if incomplete else "available",
        },
    )


def _fingerprint_account_data(account: SnapshotAccount) -> dict[str, object]:
    return {
        "account_id": str(account.account_id),
        "archived_at": _timestamp(account.archived_at),
        "emergency_reserve_target_eur": _decimal(account.emergency_reserve_target_eur),
        "name": account.name,
        "role": account.role,
    }


def _fingerprint_event_data(entry: LedgerEvent) -> dict[str, object]:
    event = entry.event
    return {
        "account_id": str(event.account_id),
        "correction_of_event_id": str(event.correction_of_event_id)
        if event.correction_of_event_id
        else None,
        "created_at": _timestamp(entry.created_at),
        "event_id": str(entry.event_id),
        "event_type": event.event_type.value,
        "legs": [_leg_data(leg) for leg in event.legs],
        "occurred_at": _timestamp(event.occurred_at),
        "reversal_of_event_id": str(event.reversal_of_event_id)
        if event.reversal_of_event_id
        else None,
        "source_group_reference": entry.group_reference,
        "source_identity": {
            "event_reference": event.source_identity.event_reference,
            "provider": event.source_identity.provider,
        },
        "source_reported_eur": (
            {
                "amount": _decimal(event.source_reported_eur.eur_amount.amount),
                "rate": _decimal(event.source_reported_eur.source_rate),
                "reported_at": _timestamp(event.source_reported_eur.reported_at),
            }
            if event.source_reported_eur
            else None
        ),
    }


def _account_data(account: SnapshotAccount) -> dict[str, object]:
    return {
        "account_id": str(account.account_id),
        "archived_at": _timestamp(account.archived_at),
        "emergency_reserve_target_eur": _decimal(account.emergency_reserve_target_eur),
        "name": account.name,
        "role": account.role,
    }


def _cash_data(balance: object) -> dict[str, object]:
    return {
        "account_id": str(balance.account_id),
        "amount": _decimal(balance.amount),
        "currency": balance.currency,
        "evidence_event_ids": list(balance.evidence_event_ids),
    }


def _position_data(position: object) -> dict[str, object]:
    return {
        **({"account_id": str(position.account_id)} if position.account_id else {}),
        "evidence_event_ids": list(position.evidence_event_ids),
        "instrument_id": position.instrument_id,
        "quantity": _decimal(position.quantity),
    }


def _lot_data(lot: object) -> dict[str, object]:
    return {
        "account_id": str(lot.account_id),
        "buy_event_id": lot.buy_event_id,
        "evidence_event_ids": list(lot.evidence_event_ids),
        "fee_event_ids": list(lot.fee_event_ids),
        "instrument_id": lot.instrument_id,
        "quantity": _decimal(lot.quantity),
        "source_currency": lot.source_currency,
        "total_basis": _decimal(lot.total_basis),
    }


def _sale_data(sale: object) -> dict[str, object]:
    return {
        "account_id": str(sale.account_id),
        "allocations": [
            {
                "allocated_basis": _decimal(allocation.allocated_basis),
                "buy_event_id": allocation.buy_event_id,
                "evidence_event_ids": list(allocation.evidence_event_ids),
                "quantity": _decimal(allocation.quantity),
                "sale_event_id": allocation.sale_event_id,
            }
            for allocation in sale.allocations
        ],
        "allocated_basis": _decimal(sale.allocated_basis),
        "evidence_event_ids": list(sale.evidence_event_ids),
        "instrument_id": sale.instrument_id,
        "proceeds": _decimal(sale.proceeds),
        "quantity": _decimal(sale.quantity),
        "realized_gain": _decimal(sale.realized_gain),
        "sale_event_id": sale.sale_event_id,
        "source_currency": sale.source_currency,
    }


def _diagnostic_data(diagnostic: AccountingDiagnostic) -> dict[str, object]:
    return {
        "account_id": str(diagnostic.account_id),
        "code": diagnostic.code,
        "evidence_event_ids": [diagnostic.event_id],
        "source_group_reference": diagnostic.source_group_reference,
    }


def _leg_data(leg: CashLeg | InstrumentLeg) -> dict[str, object]:
    if isinstance(leg, CashLeg):
        return {
            "amount": _decimal(leg.money.amount),
            "currency": leg.money.currency,
            "direction": leg.direction.value,
            "kind": "cash",
        }
    return {
        "direction": leg.direction.value,
        "instrument_id": leg.instrument_id,
        "kind": "instrument",
        "quantity": _decimal(leg.quantity.value),
    }


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone offset")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    _require_decimal(value, "decimal")
    return format(value, "f")


def _require_decimal(value: object, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
