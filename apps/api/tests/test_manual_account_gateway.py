"""Credential-free local-Supabase tests for trusted manual account workflows."""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.manual_accounts import (
    CashMovementCommand,
    CorrectionCommand,
    ManualAccountCreate,
    TransferCommand,
)
from pia_api.services.manual_accounts import (
    ManualAccountConflictError,
    TrustedManualAccountGateway,
)

pytestmark = pytest.mark.local_supabase


@pytest.fixture(scope="module")
def database_url() -> str:
    """Require explicit opt-in before using the local Supabase database."""
    if os.environ.get("PIA_RUN_LOCAL_SUPABASE_TESTS") != "1":
        pytest.skip("set PIA_RUN_LOCAL_SUPABASE_TESTS=1 to run local Supabase tests")
    return Settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _insert_auth_user(
    connection: psycopg.Connection[object], user_id: uuid.UUID, email: str
) -> None:
    connection.execute(
        """
        INSERT INTO auth.users (
            id, instance_id, aud, role, email, encrypted_password,
            email_confirmed_at, raw_app_meta_data, raw_user_meta_data,
            created_at, updated_at
        )
        VALUES (%s, '00000000-0000-0000-0000-000000000000', 'authenticated',
                'authenticated', %s, '', now(),
                '{"provider":"email","providers":["email"]}',
                '{}', now(), now())
        """,
        (user_id, email),
    )


def _as_authenticated_user(
    connection: psycopg.Connection[object], user_id: uuid.UUID
) -> None:
    connection.execute("SET LOCAL ROLE authenticated")
    connection.execute(
        "SELECT set_config('request.jwt.claim.sub', %s, true)", (str(user_id),)
    )
    connection.execute(
        "SELECT set_config('request.jwt.claim.role', 'authenticated', true)"
    )


def _gateway(database_url: str) -> TrustedManualAccountGateway:
    return TrustedManualAccountGateway(Settings(database_url=database_url))


def _movement(amount: str) -> CashMovementCommand:
    return CashMovementCommand(
        amount=amount,
        currency="EUR",
        occurred_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
    )


def _opening(amount: str) -> CashMovementCommand:
    return _movement(amount).model_copy(update={"kind": "opening_balance"})


def test_manual_account_workflows_are_append_only_atomic_and_idempotent(
    database_url: str,
) -> None:
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    gateway = _gateway(database_url)
    owner = AuthenticatedUser(id=str(owner_id), email="owner@example.test")
    other = AuthenticatedUser(id=str(other_id), email="other@example.test")

    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _insert_auth_user(
                connection, owner_id, f"manual-owner-{owner_id}@example.test"
            )
            _insert_auth_user(
                connection, other_id, f"manual-other-{other_id}@example.test"
            )

        cash = asyncio.run(
            gateway.create_account(
                owner, ManualAccountCreate(name="Wallet", role="cash")
            )
        )
        savings = asyncio.run(
            gateway.create_account(
                owner, ManualAccountCreate(name="Savings", role="savings")
            )
        )
        reserve = asyncio.run(
            gateway.create_account(
                owner,
                ManualAccountCreate(
                    name="Emergency reserve",
                    role="emergency_reserve",
                    emergency_reserve_target_eur="500.00",
                ),
            )
        )
        other_cash = asyncio.run(
            gateway.create_account(
                other, ManualAccountCreate(name="Other", role="cash")
            )
        )
        assert reserve["emergency_reserve_target_eur"] == "500.00"
        assert asyncio.run(gateway.list_accounts(other)) == [other_cash]

        opening = asyncio.run(
            gateway.record_cash_movement(
                owner, cash["id"], _opening("100.00"), "opening"
            )
        )
        assert opening is not None
        with pytest.raises(ManualAccountConflictError):
            asyncio.run(
                gateway.record_cash_movement(
                    owner, cash["id"], _opening("1.00"), "opening-again"
                )
            )
        deposited = asyncio.run(
            gateway.record_cash_movement(
                owner, cash["id"], _movement("20.00"), "deposit"
            )
        )
        withdrawn = asyncio.run(
            gateway.record_cash_movement(
                owner,
                cash["id"],
                _movement("10.00").model_copy(update={"kind": "withdrawal"}),
                "withdrawal",
            )
        )
        assert deposited is not None and withdrawn is not None
        reversal = asyncio.run(
            gateway.record_cash_movement(
                owner,
                cash["id"],
                CorrectionCommand(
                    amount="20.00",
                    currency="EUR",
                    occurred_at=datetime(2026, 8, 4, 12, 1, tzinfo=UTC),
                    target_event_id=deposited["event_ids"][0],
                    mode="reversal",
                ),
                "deposit-reversal",
            )
        )
        assert reversal is not None
        transfer = TransferCommand(
            from_account_id=cash["id"],
            to_account_id=savings["id"],
            amount="40.00",
            currency="EUR",
            occurred_at=datetime(2026, 8, 4, 12, 5, tzinfo=UTC),
        )
        first_transfer = asyncio.run(
            gateway.record_transfer(owner, transfer, "transfer-1")
        )
        retry_transfer = asyncio.run(
            gateway.record_transfer(owner, transfer, "transfer-1")
        )
        assert first_transfer == retry_transfer
        assert first_transfer is not None
        assert first_transfer["transfer_group_reference"].startswith("manual-transfer:")
        assert len(first_transfer["event_ids"]) == 2

        with pytest.raises(ManualAccountConflictError):
            asyncio.run(
                gateway.record_transfer(
                    owner,
                    transfer.model_copy(update={"amount": "41.00"}),
                    "transfer-1",
                )
            )
        assert (
            asyncio.run(
                gateway.record_transfer(
                    owner,
                    TransferCommand(
                        from_account_id=cash["id"],
                        to_account_id=other_cash["id"],
                        amount="1.00",
                        currency="EUR",
                        occurred_at=datetime(2026, 8, 4, 12, 6, tzinfo=UTC),
                    ),
                    "cross-owner",
                )
            )
            is None
        )

        archived = asyncio.run(gateway.archive_account(owner, cash["id"]))
        assert archived is not None
        assert archived["archived_at"] is not None
        with pytest.raises(ManualAccountConflictError):
            asyncio.run(
                gateway.record_cash_movement(
                    owner, cash["id"], _movement("1.00"), "after-archive"
                )
            )

        with psycopg.connect(database_url) as connection:
            grouped_events = connection.execute(
                """
                SELECT direction, cash_amount, source_group_reference
                FROM public.financial_events AS events
                JOIN public.financial_event_legs AS legs ON legs.event_id = events.id
                WHERE events.id = ANY(%s::uuid[])
                ORDER BY legs.direction
                """,
                (first_transfer["event_ids"],),
            ).fetchall()
            assert grouped_events[0][2] == grouped_events[1][2]
            assert (
                sum(
                    amount if direction == "in" else -amount
                    for direction, amount, _ in grouped_events
                )
                == 0
            )
            assert connection.execute(
                """
                SELECT count(*) FROM public.financial_events
                WHERE source_event_reference LIKE 'manual:%:transfer:%'
                """
            ).fetchone() == (2,)
            audit_metadata = connection.execute(
                """
                SELECT metadata FROM public.audit_events
                WHERE actor_id = %s AND event_type LIKE 'manual_%%'
                """,
                (owner_id,),
            ).fetchall()
            assert audit_metadata
            assert all(
                not {"amount", "currency", "name", "idempotency_key"} & set(metadata)
                for (metadata,) in audit_metadata
            )

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, other_id)
                assert (
                    connection.execute(
                        "SELECT id FROM public.financial_accounts WHERE id = %s",
                        (cash["id"],),
                    ).fetchall()
                    == []
                )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute(
                        "SELECT * FROM public.manual_ledger_idempotency_keys"
                    ).fetchall()
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)", (owner_id, other_id)
            )
