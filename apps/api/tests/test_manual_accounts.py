"""Contract tests for authenticated, append-only manual account workflows."""

from dataclasses import dataclass

from fastapi.testclient import TestClient

from pia_api.core.auth import AuthenticatedUser
from pia_api.main import create_app


@dataclass
class ManualAccountGateway:
    """Small owner-aware gateway used to prove the HTTP boundary."""

    calls: list[tuple[str, str]]

    async def list_accounts(self, user: AuthenticatedUser):
        if user.id != "owner":
            return []
        return [
            {
                "id": "account-1",
                "name": "Emergency fund",
                "role": "emergency_reserve",
                "archived_at": None,
                "emergency_reserve_target_eur": "500.00",
            }
        ]

    async def create_account(self, user: AuthenticatedUser, command: object):
        self.calls.append((user.id, "create"))
        return {
            "id": "account-2",
            "name": command.name,
            "role": command.role,
            "archived_at": None,
            "emergency_reserve_target_eur": command.emergency_reserve_target_eur,
        }

    async def update_account(
        self, user: AuthenticatedUser, account_id: str, command: object
    ):
        if user.id != "owner" or account_id != "account-1":
            return None
        self.calls.append((user.id, "update"))
        return {
            "id": account_id,
            "name": command.name or "Emergency fund",
            "role": "emergency_reserve",
            "archived_at": None,
            "emergency_reserve_target_eur": command.emergency_reserve_target_eur,
        }

    async def archive_account(self, user: AuthenticatedUser, account_id: str):
        if user.id != "owner" or account_id != "account-1":
            return None
        self.calls.append((user.id, "archive"))
        return {
            "id": account_id,
            "name": "Emergency fund",
            "role": "emergency_reserve",
            "archived_at": "2026-08-04T10:00:00Z",
            "emergency_reserve_target_eur": "500.00",
        }

    async def record_cash_movement(
        self,
        user: AuthenticatedUser,
        account_id: str,
        command: object,
        idempotency_key: str,
    ):
        if user.id != "owner" or account_id != "account-1":
            return None
        self.calls.append((user.id, f"{command.kind}:{idempotency_key}"))
        return {"event_ids": ["event-1"], "transfer_group_reference": None}

    async def record_transfer(
        self, user: AuthenticatedUser, command: object, idempotency_key: str
    ):
        if user.id != "owner":
            return None
        self.calls.append((user.id, f"transfer:{idempotency_key}"))
        return {
            "event_ids": ["event-out", "event-in"],
            "transfer_group_reference": "manual-transfer-1",
        }


class Verifier:
    async def verify(self, token: str) -> AuthenticatedUser:
        from jwt import InvalidTokenError

        if token not in {"owner-token", "other-token"}:
            raise InvalidTokenError("bad")
        user_id = "owner" if token == "owner-token" else "other"
        return AuthenticatedUser(id=user_id, email=f"{user_id}@example.test")


def _client() -> tuple[TestClient, ManualAccountGateway]:
    app = create_app()
    app.state.jwt_verifier = Verifier()
    gateway = ManualAccountGateway([])
    app.state.manual_account_gateway = gateway
    return TestClient(app), gateway


def test_manual_account_routes_require_authentication_and_preserve_decimal_strings():
    client, gateway = _client()

    assert client.get("/v1/accounts").status_code == 401
    assert client.post("/v1/accounts", json={}).status_code == 401

    listed = client.get("/v1/accounts", headers={"Authorization": "Bearer owner-token"})
    assert listed.status_code == 200
    assert listed.json()[0]["emergency_reserve_target_eur"] == "500.00"

    created = client.post(
        "/v1/accounts",
        headers={"Authorization": "Bearer owner-token"},
        json={
            "name": "Holiday reserve",
            "role": "emergency_reserve",
            "emergency_reserve_target_eur": "1000.00",
        },
    )
    assert created.status_code == 201
    assert created.json()["emergency_reserve_target_eur"] == "1000.00"
    assert gateway.calls == [("owner", "create")]


def test_manual_account_metadata_is_owner_scoped_and_validated():
    client, gateway = _client()
    headers = {"Authorization": "Bearer owner-token"}

    assert (
        client.patch(
            "/v1/accounts/account-1",
            headers=headers,
            json={"name": "", "emergency_reserve_target_eur": "500.00"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/accounts",
            headers=headers,
            json={
                "name": "Cash",
                "role": "cash",
                "emergency_reserve_target_eur": "1.00",
            },
        ).status_code
        == 422
    )
    assert (
        client.patch(
            "/v1/accounts/account-1",
            headers={"Authorization": "Bearer other-token"},
            json={"name": "Other"},
        ).status_code
        == 404
    )

    updated = client.patch(
        "/v1/accounts/account-1",
        headers=headers,
        json={"name": "Emergency fund", "emergency_reserve_target_eur": "650.00"},
    )
    assert updated.status_code == 200
    archived = client.post("/v1/accounts/account-1/archive", headers=headers)
    assert archived.status_code == 200
    assert archived.json()["archived_at"] == "2026-08-04T10:00:00Z"
    assert gateway.calls == [("owner", "update"), ("owner", "archive")]


def test_manual_cash_workflows_require_idempotency_and_never_accept_floats():
    client, gateway = _client()
    headers = {"Authorization": "Bearer owner-token", "Idempotency-Key": "key-1"}
    payload = {
        "amount": "100.00",
        "currency": "EUR",
        "occurred_at": "2026-08-04T10:00:00Z",
    }

    assert (
        client.post(
            "/v1/accounts/account-1/opening-balance",
            headers={"Authorization": "Bearer owner-token"},
            json=payload,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/accounts/account-1/deposits",
            headers=headers,
            json={**payload, "amount": 100.0},
        ).status_code
        == 422
    )

    opening = client.post(
        "/v1/accounts/account-1/opening-balance", headers=headers, json=payload
    )
    assert opening.status_code == 201
    assert opening.json() == {
        "event_ids": ["event-1"],
        "transfer_group_reference": None,
    }
    deposit = client.post(
        "/v1/accounts/account-1/deposits", headers=headers, json=payload
    )
    assert deposit.status_code == 201
    withdrawal = client.post(
        "/v1/accounts/account-1/withdrawals", headers=headers, json=payload
    )
    assert withdrawal.status_code == 201
    assert gateway.calls == [
        ("owner", "opening_balance:key-1"),
        ("owner", "deposit:key-1"),
        ("owner", "withdrawal:key-1"),
    ]
    correction = client.post(
        "/v1/accounts/account-1/corrections",
        headers=headers,
        json={
            **payload,
            "target_event_id": "event-1",
            "mode": "correction",
            "direction": "out",
        },
    )
    assert correction.status_code == 201


def test_manual_transfers_are_authenticated_owner_scoped_and_return_linked_events():
    client, gateway = _client()
    payload = {
        "from_account_id": "account-1",
        "to_account_id": "account-2",
        "amount": "40.00",
        "currency": "EUR",
        "occurred_at": "2026-08-04T10:00:00Z",
    }

    assert client.post("/v1/transfers", json=payload).status_code == 401
    assert (
        client.post(
            "/v1/transfers",
            headers={"Authorization": "Bearer owner-token"},
            json=payload,
        ).status_code
        == 422
    )
    transferred = client.post(
        "/v1/transfers",
        headers={"Authorization": "Bearer owner-token", "Idempotency-Key": "key-2"},
        json=payload,
    )
    assert transferred.status_code == 201
    assert transferred.json() == {
        "event_ids": ["event-out", "event-in"],
        "transfer_group_reference": "manual-transfer-1",
    }
    assert gateway.calls == [("owner", "transfer:key-2")]


def test_manual_account_routes_allow_the_idempotency_header_from_the_browser():
    client, _ = _client()

    response = client.options(
        "/v1/transfers",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization,content-type,idempotency-key"
            ),
        },
    )

    assert response.status_code == 200
    assert "idempotency-key" in response.headers["access-control-allow-headers"].lower()
