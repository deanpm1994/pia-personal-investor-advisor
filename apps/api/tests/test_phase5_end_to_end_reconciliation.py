"""Phase 5 synthetic reconciliation across accounting, snapshots, and HTTP."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from accounting_fixtures import (
    ACCOUNT_FIXTURES,
    ACCOUNTING_FIXTURE_HISTORY,
    CASH_ACCOUNT_ID,
    CASH_BALANCE_EXPECTATIONS,
    EXPECTED_FEE_ADJUSTED_PROCEEDS,
    EXPECTED_OWNER_NATIVE_CASH,
    EXPECTED_REALIZED_GAIN,
    EXPECTED_RESERVE_EUR_BALANCE,
    EXPECTED_RESERVE_EUR_TARGET,
    FIFO_REMAINING_LOTS,
    POSITION_EXPECTATIONS,
)
from fastapi.testclient import TestClient

from pia_api.core.auth import AuthenticatedUser
from pia_api.domain.accounting import LedgerEvent, replay_accounting
from pia_api.domain.fifo_accounting import replay_fifo_accounting
from pia_api.domain.financial_snapshots import SnapshotAccount, build_snapshot_material
from pia_api.main import create_app
from pia_api.services.financial_snapshots import SnapshotReadResult


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


def _ledger_events() -> tuple[LedgerEvent, ...]:
    return tuple(
        LedgerEvent(
            event_id=fixture.event_id,
            event=fixture.event,
            created_at=fixture.created_at,
            source_group_reference=fixture.source_group_reference,
        )
        for fixture in ACCOUNTING_FIXTURE_HISTORY
    )


def _has_float(value: object) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_has_float(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_float(item) for item in value)
    return False


class FixtureVerifier:
    async def verify(self, token: str) -> AuthenticatedUser:
        from jwt import InvalidTokenError

        owner_id = str(ACCOUNTING_FIXTURE_HISTORY[0].event.owner_id)
        if token == "owner-token":
            return AuthenticatedUser(id=owner_id, email="owner@example.test")
        if token == "other-token":
            return AuthenticatedUser(
                id="00000000-0000-0000-0000-000000000999", email=None
            )
        raise InvalidTokenError("invalid synthetic token")


@dataclass
class FixtureSnapshotGateway:
    owner_id: str
    snapshot: SnapshotReadResult

    async def get_latest(self, user: AuthenticatedUser) -> SnapshotReadResult | None:
        return self.snapshot if user.id == self.owner_id else None


def _client(snapshot: SnapshotReadResult) -> TestClient:
    app = create_app()
    app.state.jwt_verifier = FixtureVerifier()
    app.state.financial_picture_gateway = FixtureSnapshotGateway(
        owner_id=str(ACCOUNTING_FIXTURE_HISTORY[0].event.owner_id), snapshot=snapshot
    )
    return TestClient(app)


def test_synthetic_history_reconciles_from_ledger_to_snapshot_to_api() -> None:
    accounts = _accounts()
    ledger_events = _ledger_events()
    accounting = replay_accounting(accounts, ledger_events)
    fifo = replay_fifo_accounting(accounts, ledger_events)
    material = build_snapshot_material(accounts, ledger_events)

    assert {
        (balance.account_id, balance.currency): balance.amount
        for balance in accounting.account_cash_balances
    } == {
        (expectation.account_id, expectation.currency): expectation.amount
        for expectation in CASH_BALANCE_EXPECTATIONS
    }
    assert {
        balance.currency: balance.amount for balance in accounting.owner_cash_balances
    } == EXPECTED_OWNER_NATIVE_CASH
    assert sum(
        (
            balance.amount
            for balance in accounting.account_cash_balances
            if balance.account_id == CASH_ACCOUNT_ID and balance.currency == "EUR"
        ),
        Decimal("0"),
    ) == Decimal("75.0000")

    expected_position = POSITION_EXPECTATIONS[0]
    assert accounting.owner_positions[0].quantity == expected_position.quantity
    assert fifo.open_lots[0].quantity == FIFO_REMAINING_LOTS[0].quantity
    assert fifo.open_lots[0].total_basis == FIFO_REMAINING_LOTS[0].total_basis
    assert fifo.realized_sales[0].proceeds == EXPECTED_FEE_ADJUSTED_PROCEEDS
    assert fifo.realized_sales[0].realized_gain == EXPECTED_REALIZED_GAIN

    assert material.content["cash_by_currency"]["owner"] == {
        currency: format(amount, "f")
        for currency, amount in EXPECTED_OWNER_NATIVE_CASH.items()
    }
    assert material.content["positions"]["owner"] == [
        {
            "evidence_event_ids": [
                "00000000-0000-0000-0000-000000000303",
                "00000000-0000-0000-0000-000000000305",
                "00000000-0000-0000-0000-000000000307",
                "00000000-0000-0000-0000-000000000313",
            ],
            "instrument_id": expected_position.instrument_id,
            "quantity": format(expected_position.quantity, "f"),
        }
    ]
    assert material.content["reserve_progress"] == {
        "available_eur_balance": format(EXPECTED_RESERVE_EUR_BALANCE, "f"),
        "configured_target_eur": format(
            EXPECTED_RESERVE_EUR_TARGET + Decimal("250.0000"), "f"
        ),
        "status": "incomplete",
    }
    assert {diagnostic["code"] for diagnostic in material.content["diagnostics"]} == {
        "SNAPSHOT_RESERVE_NON_EUR_BALANCE"
    }
    assert set(material.content["evidence_event_ids"]) == {
        str(entry.event_id) for entry in ledger_events
    }

    client = _client(
        SnapshotReadResult(
            snapshot_id="synthetic-snapshot",
            input_fingerprint=material.fingerprint,
            as_of="2026-07-17T12:00:00Z",
            refreshed_at="2026-08-09T12:00:00Z",
            input_counts=material.input_counts,
            content=material.content,
            is_fresh=True,
        )
    )
    assert client.get("/v1/financial-picture").status_code == 401
    assert (
        client.get(
            "/v1/financial-picture", headers={"Authorization": "Bearer other-token"}
        ).status_code
        == 404
    )

    response = client.get(
        "/v1/financial-picture", headers={"Authorization": "Bearer owner-token"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "incomplete"
    assert payload["cash_by_currency"] == material.content["cash_by_currency"]
    assert payload["positions"] == material.content["positions"]
    assert payload["fifo"] == material.content["fifo"]
    assert payload["reserve_progress"] == material.content["reserve_progress"]
    assert payload["diagnostics"] == material.content["diagnostics"]
    assert payload["evidence_event_ids"] == material.content["evidence_event_ids"]
    assert not _has_float(payload)


def test_snapshot_material_reuses_unchanged_inputs_and_changes_with_metadata() -> None:
    accounts = _accounts()
    ledger_events = _ledger_events()
    baseline = build_snapshot_material(accounts, ledger_events)
    reordered = build_snapshot_material(
        tuple(reversed(accounts)), tuple(reversed(ledger_events))
    )
    renamed = build_snapshot_material(
        (replace(accounts[0], name="Renamed synthetic account"), *accounts[1:]),
        ledger_events,
    )

    assert reordered.fingerprint == baseline.fingerprint
    assert reordered.content == baseline.content
    assert renamed.fingerprint != baseline.fingerprint
