"""Local-Supabase integration checks for the P2.3 ownership baseline."""

import os
import uuid

import psycopg
import pytest
from financial_fixtures import (
    ACCOUNT_ID,
    DUPLICATE_DEPOSIT,
    FIXTURE_HISTORY,
    INSTRUMENT_ID,
    OWNER_ID,
)

from pia_api.core.config import Settings
from pia_api.domain.financial_events import CashLeg

pytestmark = pytest.mark.local_supabase


@pytest.fixture(scope="module")
def database_url() -> str:
    """Require an explicit opt-in before touching the local Supabase database."""
    if os.environ.get("PIA_RUN_LOCAL_SUPABASE_TESTS") != "1":
        pytest.skip("set PIA_RUN_LOCAL_SUPABASE_TESTS=1 to run local Supabase tests")
    return Settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _insert_auth_user(
    connection: psycopg.Connection[object], user_id: uuid.UUID, email: str
) -> None:
    """Create synthetic local Auth users so the production trigger is exercised."""
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


def test_profiles_are_created_synced_and_isolated(database_url: str) -> None:
    """Prove Auth synchronization, anonymous denial, and cross-user RLS denial."""
    first_user, second_user = uuid.uuid4(), uuid.uuid4()
    first_email = f"first-{first_user}@example.test"
    second_email = f"second-{second_user}@example.test"
    updated_first_email = f"first-updated-{first_user}@example.test"

    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(admin_connection, first_user, first_email)
            _insert_auth_user(admin_connection, second_user, second_email)
            profiles = admin_connection.execute(
                "SELECT id, email FROM public.profiles "
                "WHERE id IN (%s, %s) ORDER BY email",
                (first_user, second_user),
            ).fetchall()
            assert profiles == [
                (first_user, first_email),
                (second_user, second_email),
            ]

            admin_connection.execute(
                "UPDATE auth.users SET email = %s WHERE id = %s",
                (updated_first_email, first_user),
            )
            assert admin_connection.execute(
                "SELECT email FROM public.profiles WHERE id = %s", (first_user,)
            ).fetchone() == (updated_first_email,)

        with psycopg.connect(database_url) as anonymous_connection:
            with anonymous_connection.transaction():
                anonymous_connection.execute("SET LOCAL ROLE anon")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    anonymous_connection.execute(
                        "SELECT id FROM public.profiles"
                    ).fetchall()

        with psycopg.connect(database_url) as user_connection:
            with user_connection.transaction():
                _as_authenticated_user(user_connection, first_user)
                assert user_connection.execute(
                    "SELECT id FROM public.profiles ORDER BY id"
                ).fetchall() == [(first_user,)]
                assert (
                    user_connection.execute(
                        "SELECT id FROM public.profiles WHERE id = %s", (second_user,)
                    ).fetchall()
                    == []
                )
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM public.audit_events WHERE actor_id IN (%s, %s)",
                (first_user, second_user),
            )
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (first_user, second_user),
            )


def test_profile_migration_downgrades_and_upgrades(database_url: str) -> None:
    """Exercise the approved migration's rollback and repeatable upgrade path."""
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    command.downgrade(config, "20260714_03")
    command.upgrade(config, "head")


def test_financial_ledger_is_owner_scoped_and_append_only(database_url: str) -> None:
    """Prove ledger clients may append their facts but never rewrite history."""
    first_user, second_user = uuid.uuid4(), uuid.uuid4()
    first_account_id: uuid.UUID
    first_event_id: uuid.UUID

    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection, first_user, f"ledger-first-{first_user}@example.test"
            )
            _insert_auth_user(
                admin_connection,
                second_user,
                f"ledger-second-{second_user}@example.test",
            )

        with psycopg.connect(database_url) as anonymous_connection:
            with anonymous_connection.transaction():
                anonymous_connection.execute("SET LOCAL ROLE anon")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with anonymous_connection.transaction():
                        anonymous_connection.execute(
                            "SELECT id FROM public.financial_accounts"
                        ).fetchall()
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with anonymous_connection.transaction():
                        anonymous_connection.execute(
                            "INSERT INTO public.financial_accounts (user_id) "
                            "VALUES (%s)",
                            (first_user,),
                        )

        with psycopg.connect(database_url) as first_connection:
            with first_connection.transaction():
                _as_authenticated_user(first_connection, first_user)
                first_account_id = first_connection.execute(
                    """
                    INSERT INTO public.financial_accounts (user_id)
                    VALUES (%s)
                    RETURNING id
                    """,
                    (first_user,),
                ).fetchone()[0]
                first_connection.execute(
                    """
                    INSERT INTO public.financial_instruments (user_id, instrument_id)
                    VALUES (%s, 'US0378331005')
                    """,
                    (first_user,),
                )
                first_event_id = first_connection.execute(
                    """
                    INSERT INTO public.financial_events (
                        user_id, account_id, source_provider, source_event_reference,
                        event_type, occurred_at, source_reported_eur_amount,
                        source_reported_eur_rate, source_reported_eur_reported_at
                    )
                    VALUES (%s, %s, 'fixture', 'buy-1', 'buy', now(),
                            12.3400, 1.0000, now())
                    RETURNING id
                    """,
                    (first_user, first_account_id),
                ).fetchone()[0]
                first_connection.execute(
                    """
                    INSERT INTO public.financial_event_legs (
                        event_id, user_id, account_id, position, leg_kind, direction,
                        cash_amount, cash_currency
                    )
                    VALUES (%s, %s, %s, 1, 'cash', 'out', 12.3400, 'EUR')
                    """,
                    (first_event_id, first_user, first_account_id),
                )
                first_connection.execute(
                    """
                    INSERT INTO public.financial_event_legs (
                        event_id, user_id, account_id, position, leg_kind, direction,
                        instrument_id, quantity
                    )
                    VALUES (%s, %s, %s, 2, 'instrument', 'in', 'US0378331005', 0.125000)
                    """,
                    (first_event_id, first_user, first_account_id),
                )

        with psycopg.connect(database_url) as first_connection:
            with first_connection.transaction():
                _as_authenticated_user(first_connection, first_user)
                assert first_connection.execute(
                    "SELECT id FROM public.financial_events"
                ).fetchall() == [(first_event_id,)]
                for statement, parameters in (
                    (
                        "UPDATE public.financial_accounts SET created_at = now() "
                        "WHERE id = %s",
                        (first_account_id,),
                    ),
                    (
                        "DELETE FROM public.financial_instruments "
                        "WHERE instrument_id = 'US0378331005'",
                        (),
                    ),
                    (
                        "UPDATE public.financial_events SET occurred_at = now() "
                        "WHERE id = %s",
                        (first_event_id,),
                    ),
                    (
                        "DELETE FROM public.financial_event_legs WHERE event_id = %s",
                        (first_event_id,),
                    ),
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with first_connection.transaction():
                            first_connection.execute(statement, parameters)

        with psycopg.connect(database_url) as second_connection:
            with second_connection.transaction():
                _as_authenticated_user(second_connection, second_user)
                assert (
                    second_connection.execute(
                        "SELECT id FROM public.financial_events"
                    ).fetchall()
                    == []
                )
                with pytest.raises(psycopg.errors.ForeignKeyViolation):
                    with second_connection.transaction():
                        second_connection.execute(
                            """
                        INSERT INTO public.financial_events (
                            user_id, account_id, source_provider,
                            source_event_reference,
                            event_type, occurred_at
                        )
                        VALUES (%s, %s, 'fixture', 'cross-user', 'deposit', now())
                        """,
                            (second_user, first_account_id),
                        )
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (first_user, second_user),
            )


def test_financial_ledger_constraints_reject_invalid_history(
    database_url: str,
) -> None:
    """Prove database constraints retain source facts and complete event shapes."""
    owner_id = uuid.uuid4()
    account_id: uuid.UUID
    original_event_id: uuid.UUID

    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection,
                owner_id,
                f"ledger-constraints-{owner_id}@example.test",
            )
        with psycopg.connect(database_url) as admin_connection:
            with admin_connection.transaction():
                account_id = admin_connection.execute(
                    """
                    INSERT INTO public.financial_accounts (user_id)
                    VALUES (%s)
                    RETURNING id
                    """,
                    (owner_id,),
                ).fetchone()[0]
                admin_connection.execute(
                    """
                    INSERT INTO public.financial_instruments (user_id, instrument_id)
                    VALUES (%s, 'US0378331005')
                    """,
                    (owner_id,),
                )
                original_event_id = admin_connection.execute(
                    """
                    INSERT INTO public.financial_events (
                        user_id, account_id, source_provider, source_event_reference,
                        event_type, occurred_at
                    )
                    VALUES (%s, %s, 'fixture', 'original', 'deposit', now())
                    RETURNING id
                    """,
                    (owner_id, account_id),
                ).fetchone()[0]
                admin_connection.execute(
                    """
                    INSERT INTO public.financial_event_legs (
                        event_id, user_id, account_id, position, leg_kind, direction,
                        cash_amount, cash_currency
                    )
                    VALUES (%s, %s, %s, 1, 'cash', 'in', 100.00, 'EUR')
                    """,
                    (original_event_id, owner_id, account_id),
                )

                with pytest.raises(psycopg.errors.UniqueViolation):
                    with admin_connection.transaction():
                        admin_connection.execute(
                            """
                            INSERT INTO public.financial_events (
                                user_id, account_id, source_provider,
                                source_event_reference, event_type, occurred_at
                            )
                            VALUES (%s, %s, 'fixture', 'original', 'deposit', now())
                            """,
                            (owner_id, account_id),
                        )
                with pytest.raises(psycopg.errors.CheckViolation):
                    with admin_connection.transaction():
                        admin_connection.execute(
                            """
                            INSERT INTO public.financial_events (
                                user_id, account_id, source_provider,
                                source_event_reference, event_type, occurred_at,
                                source_reported_eur_amount
                            )
                            VALUES (%s, %s, 'fixture', 'partial-eur-evidence',
                                    'deposit', now(), 1)
                            """,
                            (owner_id, account_id),
                        )
                with pytest.raises(psycopg.errors.CheckViolation):
                    with admin_connection.transaction():
                        admin_connection.execute(
                            """
                            INSERT INTO public.financial_event_legs (
                                event_id, user_id, account_id, position, leg_kind,
                                direction, cash_amount, cash_currency, instrument_id,
                                quantity
                            )
                            VALUES (%s, %s, %s, 2, 'cash', 'in', 1, 'EUR',
                                    'US0378331005', 1)
                            """,
                            (original_event_id, owner_id, account_id),
                        )

        with psycopg.connect(database_url) as connection:
            with pytest.raises(psycopg.errors.RaiseException):
                with connection.transaction():
                    invalid_event_id = connection.execute(
                        """
                        INSERT INTO public.financial_events (
                            user_id, account_id, source_provider,
                            source_event_reference,
                            event_type, occurred_at
                        )
                        VALUES (%s, %s, 'fixture', 'invalid-buy', 'buy', now())
                        RETURNING id
                        """,
                        (owner_id, account_id),
                    ).fetchone()[0]
                    connection.execute(
                        """
                        INSERT INTO public.financial_event_legs (
                            event_id, user_id, account_id, position, leg_kind,
                            direction,
                            cash_amount, cash_currency
                        )
                        VALUES (%s, %s, %s, 1, 'cash', 'out', 1, 'EUR')
                        """,
                        (invalid_event_id, owner_id, account_id),
                    )
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id = %s", (owner_id,)
            )


def test_synthetic_financial_fixture_history_persists_without_reinterpretation(
    database_url: str,
) -> None:
    """Persist the P3.4 facts exactly; FIFO expectations stay outside the ledger."""
    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection, OWNER_ID, f"fixture-owner-{OWNER_ID}@example.test"
            )

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO public.financial_accounts (id, user_id)
                    VALUES (%s, %s)
                    """,
                    (ACCOUNT_ID, OWNER_ID),
                )
                connection.execute(
                    """
                    INSERT INTO public.financial_instruments (user_id, instrument_id)
                    VALUES (%s, %s)
                    """,
                    (OWNER_ID, INSTRUMENT_ID),
                )
                for fixture in FIXTURE_HISTORY:
                    event = fixture.event
                    evidence = event.source_reported_eur
                    connection.execute(
                        """
                        INSERT INTO public.financial_events (
                            id, user_id, account_id, source_provider,
                            source_event_reference, event_type, occurred_at,
                            source_reported_eur_amount, source_reported_eur_rate,
                            source_reported_eur_reported_at, correction_of_event_id,
                            reversal_of_event_id
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            fixture.event_id,
                            event.owner_id,
                            event.account_id,
                            event.source_identity.provider,
                            event.source_identity.event_reference,
                            event.event_type,
                            event.occurred_at,
                            evidence.eur_amount.amount if evidence else None,
                            evidence.source_rate if evidence else None,
                            evidence.reported_at if evidence else None,
                            event.correction_of_event_id,
                            event.reversal_of_event_id,
                        ),
                    )
                    for position, leg in enumerate(event.legs, start=1):
                        if isinstance(leg, CashLeg):
                            connection.execute(
                                """
                                INSERT INTO public.financial_event_legs (
                                    event_id, user_id, account_id, position, leg_kind,
                                    direction, cash_amount, cash_currency
                                )
                                VALUES (%s, %s, %s, %s, 'cash', %s, %s, %s)
                                """,
                                (
                                    fixture.event_id,
                                    event.owner_id,
                                    event.account_id,
                                    position,
                                    leg.direction,
                                    leg.money.amount,
                                    leg.money.currency,
                                ),
                            )
                        else:
                            connection.execute(
                                """
                                INSERT INTO public.financial_event_legs (
                                    event_id, user_id, account_id, position, leg_kind,
                                    direction, instrument_id, quantity
                                )
                                VALUES (%s, %s, %s, %s, 'instrument', %s, %s, %s)
                                """,
                                (
                                    fixture.event_id,
                                    event.owner_id,
                                    event.account_id,
                                    position,
                                    leg.direction,
                                    leg.instrument_id,
                                    leg.quantity.value,
                                ),
                            )

                with pytest.raises(psycopg.errors.UniqueViolation):
                    with connection.transaction():
                        duplicate = DUPLICATE_DEPOSIT.event
                        connection.execute(
                            """
                            INSERT INTO public.financial_events (
                                id, user_id, account_id, source_provider,
                                source_event_reference, event_type, occurred_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                DUPLICATE_DEPOSIT.event_id,
                                duplicate.owner_id,
                                duplicate.account_id,
                                duplicate.source_identity.provider,
                                duplicate.source_identity.event_reference,
                                duplicate.event_type,
                                duplicate.occurred_at,
                            ),
                        )

                assert connection.execute(
                    "SELECT count(*) FROM public.financial_events WHERE user_id = %s",
                    (OWNER_ID,),
                ).fetchone() == (len(FIXTURE_HISTORY),)
                assert connection.execute(
                    """
                    SELECT cash_amount FROM public.financial_event_legs
                    WHERE event_id = %s AND position = 1
                    """,
                    (FIXTURE_HISTORY[6].event_id,),
                ).fetchone() == (FIXTURE_HISTORY[6].event.legs[0].money.amount,)
                assert connection.execute(
                    """
                    SELECT correction_of_event_id, reversal_of_event_id
                    FROM public.financial_events
                    WHERE id IN (%s, %s) ORDER BY id
                    """,
                    (FIXTURE_HISTORY[-2].event_id, FIXTURE_HISTORY[-1].event_id),
                ).fetchall() == [
                    (FIXTURE_HISTORY[0].event_id, None),
                    (None, FIXTURE_HISTORY[0].event_id),
                ]
                assert connection.execute(
                    """
                    SELECT cash_amount FROM public.financial_event_legs
                    WHERE event_id = %s
                    """,
                    (FIXTURE_HISTORY[0].event_id,),
                ).fetchone() == (FIXTURE_HISTORY[0].event.legs[0].money.amount,)
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id = %s", (OWNER_ID,)
            )


def test_audit_events_and_raw_imports_are_owner_scoped(database_url: str) -> None:
    """Prove audit history and private Storage objects reject other users."""
    first_user, second_user = uuid.uuid4(), uuid.uuid4()

    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection, first_user, f"audit-first-{first_user}@example.test"
            )
            _insert_auth_user(
                admin_connection,
                second_user,
                f"audit-second-{second_user}@example.test",
            )
            event_id = admin_connection.execute(
                """
                INSERT INTO public.audit_events (actor_id, event_type, metadata)
                VALUES (%s, 'raw_import.accessed', '{"source":"test"}')
                RETURNING id
                """,
                (first_user,),
            ).fetchone()[0]

        with psycopg.connect(database_url) as anonymous_connection:
            with anonymous_connection.transaction():
                anonymous_connection.execute("SET LOCAL ROLE anon")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    anonymous_connection.execute(
                        "SELECT id FROM public.audit_events"
                    ).fetchall()

        with psycopg.connect(database_url) as first_connection:
            with first_connection.transaction():
                _as_authenticated_user(first_connection, first_user)
                assert first_connection.execute(
                    "SELECT id FROM public.audit_events"
                ).fetchall() == [(event_id,)]
                first_connection.execute(
                    """
                    INSERT INTO storage.objects (bucket_id, name, owner)
                    VALUES ('raw-imports', %s, %s)
                    """,
                    (f"{first_user}/fixture.csv", first_user),
                )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    first_connection.execute(
                        "DELETE FROM public.audit_events WHERE id = %s", (event_id,)
                    )

        with psycopg.connect(database_url) as second_connection:
            with second_connection.transaction():
                _as_authenticated_user(second_connection, second_user)
                assert (
                    second_connection.execute(
                        "SELECT id FROM public.audit_events"
                    ).fetchall()
                    == []
                )
                assert (
                    second_connection.execute(
                        "SELECT name FROM storage.objects "
                        "WHERE bucket_id = 'raw-imports'"
                    ).fetchall()
                    == []
                )
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (first_user, second_user),
            )
