"""Credential-free local-Supabase tests for immutable financial snapshots."""

import asyncio
import os
import uuid

import psycopg
import pytest

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.services.financial_snapshots import (
    SnapshotRefreshError,
    TrustedSnapshotGateway,
)

pytestmark = pytest.mark.local_supabase


@pytest.fixture(scope="module")
def database_url() -> str:
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


def _create_owner_ledger(
    connection: psycopg.Connection[object], user_id: uuid.UUID
) -> uuid.UUID:
    account_id = connection.execute(
        """
        INSERT INTO public.financial_accounts (user_id, name, role)
        VALUES (%s, 'Snapshot test cash', 'cash')
        RETURNING id
        """,
        (user_id,),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO public.financial_events (
            user_id, account_id, source_provider, source_event_reference,
            event_type, occurred_at
        )
        VALUES (%s, %s, 'snapshot-test', 'opening', 'deposit', now())
        """,
        (user_id, account_id),
    )
    event_id = connection.execute(
        """
        SELECT id FROM public.financial_events
        WHERE user_id = %s AND account_id = %s AND source_event_reference = 'opening'
        """,
        (user_id, account_id),
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO public.financial_event_legs (
            event_id, user_id, account_id, position, leg_kind, direction,
            cash_amount, cash_currency
        )
        VALUES (%s, %s, %s, 1, 'cash', 'in', 100.0000, 'EUR')
        """,
        (event_id, user_id, account_id),
    )
    return account_id


def test_snapshot_refresh_is_idempotent_immutable_and_owner_scoped(
    database_url: str,
) -> None:
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _insert_auth_user(connection, owner_id, f"snapshot-owner-{owner_id}@test")
            _insert_auth_user(connection, other_id, f"snapshot-other-{other_id}@test")
            with connection.transaction():
                account_id = _create_owner_ledger(connection, owner_id)

        gateway = TrustedSnapshotGateway(Settings(database_url=database_url))
        owner = AuthenticatedUser(id=str(owner_id), email=None)
        first = asyncio.run(gateway.refresh(owner))
        second = asyncio.run(gateway.refresh(owner))

        assert first.snapshot_id == second.snapshot_id
        assert first.reused is False
        assert second.reused is True

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                connection.execute("SET LOCAL ROLE anon")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute("SELECT id FROM public.financial_snapshots")

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, owner_id)
                snapshot = connection.execute(
                    """
                    SELECT id::text, status, input_counts, content
                    FROM public.financial_snapshots
                    """
                ).fetchone()
                assert snapshot[0] == first.snapshot_id
                assert snapshot[1] == "completed"
                assert snapshot[2] == {"accounts": 1, "events": 1, "legs": 1}
                assert snapshot[3]["cash_by_currency"]["owner"] == {"EUR": "100.0000"}
                for statement in (
                    "UPDATE public.financial_snapshots SET status = 'failed'",
                    "DELETE FROM public.financial_snapshots",
                    """
                    INSERT INTO public.financial_snapshots (
                        user_id, input_fingerprint, input_watermark, input_counts,
                        content
                    ) VALUES (
                        current_setting('request.jwt.claim.sub')::uuid,
                        repeat('0', 64), '{}', '{}', '{}'
                    )
                    """,
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with connection.transaction():
                            connection.execute(statement)

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, other_id)
                assert (
                    connection.execute(
                        "SELECT id FROM public.financial_snapshots"
                    ).fetchall()
                    == []
                )

        with psycopg.connect(database_url, autocommit=True) as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO public.financial_events (
                        user_id, account_id, source_provider, source_event_reference,
                        event_type, occurred_at
                    ) VALUES (
                        %s, %s, 'snapshot-test', 'changed-ledger', 'deposit', now()
                    )
                    """,
                    (owner_id, account_id),
                )
                changed_event = connection.execute(
                    """
                    SELECT id FROM public.financial_events
                    WHERE user_id = %s AND source_event_reference = 'changed-ledger'
                    """,
                    (owner_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO public.financial_event_legs (
                        event_id, user_id, account_id, position, leg_kind, direction,
                        cash_amount, cash_currency
                    ) VALUES (%s, %s, %s, 1, 'cash', 'in', 1.0000, 'EUR')
                    """,
                    (changed_event, owner_id, account_id),
                )

        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                f"""
                CREATE FUNCTION public.reject_snapshot_test_insert()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.user_id = '{owner_id}'::uuid THEN
                        RAISE EXCEPTION 'forced snapshot persistence failure';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
            connection.execute(
                """
                CREATE TRIGGER financial_snapshots_test_failure
                BEFORE INSERT ON public.financial_snapshots
                FOR EACH ROW EXECUTE FUNCTION public.reject_snapshot_test_insert()
                """
            )
        with pytest.raises(SnapshotRefreshError):
            asyncio.run(gateway.refresh(owner))
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT count(*) FROM public.financial_snapshots WHERE user_id = %s",
                (owner_id,),
            ).fetchone() == (1,)
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DROP TRIGGER financial_snapshots_test_failure "
                "ON public.financial_snapshots"
            )
            connection.execute("DROP FUNCTION public.reject_snapshot_test_insert()")

        changed = asyncio.run(gateway.refresh(owner))
        assert changed.snapshot_id != first.snapshot_id
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT count(*) FROM public.financial_snapshots WHERE user_id = %s",
                (owner_id,),
            ).fetchone() == (2,)
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (owner_id, other_id),
            )


def test_snapshot_migration_downgrades_and_upgrades(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    command.downgrade(config, "20260804_09")
    command.upgrade(config, "head")
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT to_regclass('public.financial_snapshots')"
        ).fetchone() == ("financial_snapshots",)
