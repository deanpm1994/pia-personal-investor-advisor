"""HTTP contracts for the authenticated financial-picture snapshot API."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from accounting_fixtures import ACCOUNT_FIXTURES, ACCOUNTING_FIXTURE_HISTORY
from fastapi.testclient import TestClient

from pia_api.api.financial_picture import _response
from pia_api.core.auth import AuthenticatedUser
from pia_api.domain.accounting import LedgerEvent
from pia_api.domain.financial_snapshots import SnapshotAccount, build_snapshot_material
from pia_api.main import create_app
from pia_api.services.financial_snapshots import (
    SnapshotReadResult,
    SnapshotRefreshError,
    SnapshotRefreshResult,
)


class Verifier:
    async def verify(self, token: str) -> AuthenticatedUser:
        from jwt import InvalidTokenError

        if token not in {"owner-token", "other-token"}:
            raise InvalidTokenError("bad")
        user_id = "owner" if token == "owner-token" else "other"
        return AuthenticatedUser(id=user_id, email=f"{user_id}@example.test")


def _content(
    *, diagnostics: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "account_summaries": [
            {
                "account_id": "account-1",
                "name": "Emergency reserve",
                "role": "emergency_reserve",
                "archived_at": None,
                "emergency_reserve_target_eur": "1500.0000",
            }
        ],
        "cash_by_currency": {
            "accounts": [
                {
                    "account_id": "account-1",
                    "amount": "1250.0000",
                    "currency": "EUR",
                    "evidence_event_ids": ["event-1"],
                }
            ],
            "owner": {"EUR": "1250.0000"},
        },
        "diagnostics": diagnostics or [],
        "evidence_event_ids": ["event-1"],
        "fifo": {"open_lots": [], "realized_sales": []},
        "positions": {"accounts": [], "owner": []},
        "reserve_progress": {
            "available_eur_balance": "1250.0000",
            "configured_target_eur": "1500.0000",
            "status": "available",
        },
    }


def _snapshot(
    *,
    fresh: bool = True,
    event_count: int = 1,
    diagnostics: list[dict[str, object]] | None = None,
) -> SnapshotReadResult:
    return SnapshotReadResult(
        snapshot_id="snapshot-1",
        input_fingerprint="a" * 64,
        as_of="2026-08-07T10:00:00Z" if event_count else None,
        refreshed_at="2026-08-07T10:05:00Z",
        input_counts={"accounts": 1, "events": event_count, "legs": event_count},
        content=_content(diagnostics=diagnostics),
        is_fresh=fresh,
    )


@dataclass
class FinancialPictureGateway:
    records: dict[str, SnapshotReadResult | None]
    refresh_error: bool = False
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def refresh(self, user: AuthenticatedUser) -> SnapshotRefreshResult:
        self.calls.append((user.id, "refresh"))
        if self.refresh_error:
            raise SnapshotRefreshError("persistence failed")
        return SnapshotRefreshResult(
            snapshot_id="snapshot-1", fingerprint="a" * 64, reused=True
        )

    async def get_latest(self, user: AuthenticatedUser) -> SnapshotReadResult | None:
        self.calls.append((user.id, "get_latest"))
        return self.records.get(user.id)


def _client(
    records: dict[str, SnapshotReadResult | None], *, refresh_error: bool = False
) -> tuple[TestClient, FinancialPictureGateway]:
    app = create_app()
    app.state.jwt_verifier = Verifier()
    gateway = FinancialPictureGateway(records, refresh_error)
    app.state.financial_picture_gateway = gateway
    return TestClient(app), gateway


def _has_float(value: object) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_has_float(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_float(item) for item in value)
    return False


def test_financial_picture_refresh_is_authenticated_idempotent_and_decimal_safe():
    client, gateway = _client({"owner": _snapshot()})

    assert client.post("/v1/financial-picture/refresh").status_code == 401
    response = client.post(
        "/v1/financial-picture/refresh",
        headers={"Authorization": "Bearer owner-token"},
    )

    assert response.status_code == 200
    assert response.json()["snapshot_id"] == "snapshot-1"
    assert response.json()["refresh_reused"] is True
    assert response.json()["freshness"] == {"status": "fresh"}
    assert response.json()["cash_by_currency"]["owner"]["EUR"] == "1250.0000"
    assert not _has_float(response.json())
    assert gateway.calls == [("owner", "refresh"), ("owner", "get_latest")]


def test_financial_picture_read_distinguishes_each_snapshot_state():
    client, _ = _client(
        {
            "owner": None,
            "other": _snapshot(),
        }
    )

    assert client.get("/v1/financial-picture").status_code == 401
    absent = client.get(
        "/v1/financial-picture", headers={"Authorization": "Bearer owner-token"}
    )
    assert absent.status_code == 404
    assert absent.json()["detail"]["code"] == "FINANCIAL_PICTURE_NO_SNAPSHOT"
    assert (
        client.get(
            "/v1/financial-picture", headers={"Authorization": "Bearer other-token"}
        ).status_code
        == 200
    )

    client, _ = _client({"owner": _snapshot(fresh=False)})
    stale = client.get(
        "/v1/financial-picture", headers={"Authorization": "Bearer owner-token"}
    )
    assert stale.json()["state"] == "stale"
    assert stale.json()["freshness"] == {"status": "stale"}

    diagnostic = {
        "account_id": "account-1",
        "code": "FIFO_OVERSELL",
        "evidence_event_ids": ["event-1"],
        "source_group_reference": None,
    }
    client, _ = _client({"owner": _snapshot(diagnostics=[diagnostic])})
    incomplete = client.get(
        "/v1/financial-picture", headers={"Authorization": "Bearer owner-token"}
    )
    assert incomplete.json()["state"] == "incomplete"
    assert incomplete.json()["completeness"] == {
        "status": "incomplete",
        "diagnostic_count": 1,
    }

    client, _ = _client({"owner": _snapshot(event_count=0)})
    empty = client.get(
        "/v1/financial-picture", headers={"Authorization": "Bearer owner-token"}
    )
    assert empty.json()["state"] == "no_ledger_data"
    assert empty.json()["as_of"] is None


def test_financial_picture_refresh_failure_is_actionable_and_owner_isolated():
    client, gateway = _client({"owner": _snapshot()}, refresh_error=True)

    failed = client.post(
        "/v1/financial-picture/refresh",
        headers={"Authorization": "Bearer owner-token"},
    )
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "FINANCIAL_PICTURE_REFRESH_FAILED"
    assert gateway.calls == [("owner", "refresh")]

    client, _ = _client({"owner": _snapshot(), "other": None})
    assert (
        client.get(
            "/v1/financial-picture", headers={"Authorization": "Bearer other-token"}
        ).status_code
        == 404
    )


def test_response_reconciles_the_hand_worked_accounting_snapshot_fixture():
    owner_id = ACCOUNTING_FIXTURE_HISTORY[0].event.owner_id
    accounts = tuple(
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
    events = tuple(
        LedgerEvent(
            event_id=fixture.event_id,
            event=fixture.event,
            created_at=fixture.created_at,
            source_group_reference=fixture.source_group_reference,
        )
        for fixture in ACCOUNTING_FIXTURE_HISTORY
    )
    material = build_snapshot_material(accounts, events, owner_id=owner_id)

    response = _response(
        SnapshotReadResult(
            snapshot_id="snapshot-fixture",
            input_fingerprint=material.fingerprint,
            as_of="2026-07-17T12:00:00Z",
            refreshed_at="2026-08-08T10:00:00Z",
            input_counts=material.input_counts,
            content=material.content,
            is_fresh=True,
        )
    ).model_dump(mode="json")

    assert response["state"] == "incomplete"
    assert response["cash_by_currency"] == material.content["cash_by_currency"]
    assert response["positions"] == material.content["positions"]
    assert response["fifo"] == material.content["fifo"]
    assert response["reserve_progress"] == material.content["reserve_progress"]
    assert response["diagnostics"] == material.content["diagnostics"]
    assert response["evidence_event_ids"] == material.content["evidence_event_ids"]
    assert not _has_float(response)
