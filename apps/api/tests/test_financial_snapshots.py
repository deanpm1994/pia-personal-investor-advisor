"""Contract tests for immutable, reproducible financial snapshot material."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from accounting_fixtures import (
    ACCOUNT_FIXTURES,
    ACCOUNTING_FIXTURE_HISTORY,
    INVALID_OVERSELL,
)

from pia_api.domain.accounting import LedgerEvent
from pia_api.domain.financial_snapshots import (
    SnapshotAccount,
    build_snapshot_material,
)


def _accounts() -> tuple[SnapshotAccount, ...]:
    owner_id = ACCOUNTING_FIXTURE_HISTORY[0].event.owner_id
    return tuple(
        SnapshotAccount(
            account_id=fixture.account_id,
            owner_id=owner_id,
            name=f"Synthetic {fixture.role.value}",
            role=fixture.role.value,
            archived_at=None,
            emergency_reserve_target_eur=fixture.emergency_reserve_target_eur,
            updated_at=datetime(2026, 7, 20, tzinfo=UTC),
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


def test_snapshot_material_is_order_independent_and_reconciles_accounting() -> None:
    accounts = _accounts()
    events = _ledger_events(*ACCOUNTING_FIXTURE_HISTORY)

    material = build_snapshot_material(accounts, events)
    shuffled = build_snapshot_material(
        tuple(reversed(accounts)), tuple(reversed(events))
    )

    assert material.fingerprint == shuffled.fingerprint
    assert material.content == shuffled.content
    assert material.input_counts == {
        "accounts": len(accounts),
        "events": len(events),
        "legs": sum(len(event.event.legs) for event in events),
    }
    assert material.as_of == datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    assert material.content["cash_by_currency"]["owner"]["EUR"] == "2160.2150"
    assert material.content["positions"]["owner"] == [
        {
            "evidence_event_ids": [
                "00000000-0000-0000-0000-000000000303",
                "00000000-0000-0000-0000-000000000305",
                "00000000-0000-0000-0000-000000000307",
                "00000000-0000-0000-0000-000000000313",
            ],
            "instrument_id": "US0378331005",
            "quantity": "3.500",
        }
    ]
    assert material.content["reserve_progress"] == {
        "available_eur_balance": "350.0000",
        "configured_target_eur": "750.0000",
        "status": "incomplete",
    }
    assert {diagnostic["code"] for diagnostic in material.content["diagnostics"]} == {
        "SNAPSHOT_RESERVE_NON_EUR_BALANCE"
    }
    assert material.content["fifo"]["realized_sales"][0]["realized_gain"] == "32.860"


def test_relevant_account_metadata_changes_snapshot_identity() -> None:
    accounts = _accounts()
    events = _ledger_events(*ACCOUNTING_FIXTURE_HISTORY)
    baseline = build_snapshot_material(accounts, events)

    renamed = tuple(
        replace(account, name="Renamed account") if index == 0 else account
        for index, account in enumerate(accounts)
    )
    changed_target = tuple(
        replace(account, emergency_reserve_target_eur=Decimal("501.0000"))
        if account.account_id == accounts[3].account_id
        else account
        for account in accounts
    )

    assert build_snapshot_material(renamed, events).fingerprint != baseline.fingerprint
    assert (
        build_snapshot_material(changed_target, events).fingerprint
        != baseline.fingerprint
    )


def test_incomplete_history_is_persistable_with_explicit_diagnostics() -> None:
    material = build_snapshot_material(
        _accounts(), _ledger_events(*ACCOUNTING_FIXTURE_HISTORY, INVALID_OVERSELL)
    )

    assert {diagnostic["code"] for diagnostic in material.content["diagnostics"]} >= {
        "ACCOUNTING_OVERSELL",
        "FIFO_OVERSELL",
    }
    assert material.content["positions"]["owner"] == []
    assert material.content["fifo"]["open_lots"] == []
