"""Local-Supabase integration checks for owner-scoped persistence boundaries."""

import asyncio
import os
import uuid
from pathlib import Path

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
from pia_api.providers.trade_republic_csv import parse_trade_republic_csv
from pia_api.services.staged_imports import TrustedStagedImportWriter

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


def _create_review_ready_import(
    connection: psycopg.Connection[object],
    user_id: uuid.UUID,
    parsed_output: str,
    *,
    trusted: bool = True,
) -> uuid.UUID:
    """Persist a synthetic review-ready import through the trusted test boundary."""
    import_id = connection.execute(
        """
        INSERT INTO public.staged_imports (
            user_id, source_provider, source_format, trusted_staged_at
        )
        VALUES (
            %s, 'trade-republic', 'trade-republic-csv-v1',
            CASE WHEN %s THEN timezone('utc', now()) END
        )
        RETURNING id
        """,
        (user_id, trusted),
    ).fetchone()[0]
    object_path = f"{user_id}/imports/{import_id}.csv"
    connection.execute(
        "INSERT INTO storage.objects (bucket_id, name, owner) VALUES "
        "('raw-imports', %s, %s)",
        (object_path, user_id),
    )
    connection.execute(
        """
        INSERT INTO public.staged_import_files (
            user_id, staged_import_id, bucket_id, object_path, filename,
            content_type, byte_size, sha256
        )
        VALUES (%s, %s, 'raw-imports', %s, 'fixture.csv', 'text/csv', 42, %s)
        """,
        (user_id, import_id, object_path, "a" * 64),
    )
    connection.execute(
        """
        INSERT INTO public.staged_import_rows (
            user_id, staged_import_id, source_row_number, source_row, parsed_output
        )
        VALUES (%s, %s, 2, '{"Transaction ID":"fixture-1"}', %s::jsonb)
        """,
        (user_id, import_id, parsed_output),
    )
    for position, state in enumerate(
        ("staged", "parsed", "validated", "review_ready"), start=1
    ):
        connection.execute(
            """
            INSERT INTO public.staged_import_state_events (
                user_id, staged_import_id, position, state
            )
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, import_id, position, state),
        )
    return import_id


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
    """Exercise rollback, repeatable upgrades, and trustworthy group backfill."""
    from alembic import command
    from alembic.config import Config

    owner_id = uuid.uuid4()
    config = Config("alembic.ini")
    try:
        command.downgrade(config, "20260716_04")
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection,
                owner_id,
                f"source-group-backfill-{owner_id}@example.test",
            )
        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                account_id = connection.execute(
                    """
                    INSERT INTO public.financial_accounts (user_id)
                    VALUES (%s)
                    RETURNING id
                    """,
                    (owner_id,),
                ).fetchone()[0]
                for reference, event_type, direction, amount in (
                    ("legacy-trade-1:base", "deposit", "in", "100.00"),
                    ("legacy-trade-1:fee", "fee", "out", "1.00"),
                    (
                        "legacy-trade-1:withholding-tax",
                        "withholding_tax",
                        "out",
                        "2.00",
                    ),
                    ("ambiguous:other", "deposit", "in", "3.00"),
                ):
                    event_id = connection.execute(
                        """
                        INSERT INTO public.financial_events (
                            user_id, account_id, source_provider,
                            source_event_reference, event_type, occurred_at
                        )
                        VALUES (%s, %s, 'trade-republic', %s, %s, now())
                        RETURNING id
                        """,
                        (owner_id, account_id, reference, event_type),
                    ).fetchone()[0]
                    connection.execute(
                        """
                        INSERT INTO public.financial_event_legs (
                            event_id, user_id, account_id, position, leg_kind,
                            direction, cash_amount, cash_currency
                        )
                        VALUES (%s, %s, %s, 1, 'cash', %s, %s, 'EUR')
                        """,
                        (event_id, owner_id, account_id, direction, amount),
                    )

        command.upgrade(config, "head")

        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                """
                SELECT source_event_reference, source_group_reference, cash_amount
                FROM public.financial_events
                JOIN public.financial_event_legs
                    ON financial_event_legs.event_id = financial_events.id
                WHERE financial_events.user_id = %s
                ORDER BY source_event_reference
                """,
                (owner_id,),
            ).fetchall() == [
                ("ambiguous:other", None, 3),
                ("legacy-trade-1:base", "legacy-trade-1", 100),
                ("legacy-trade-1:fee", "legacy-trade-1", 1),
                (
                    "legacy-trade-1:withholding-tax",
                    "legacy-trade-1",
                    2,
                ),
            ]
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id = %s", (owner_id,)
            )


def test_staged_imports_are_owner_scoped_and_not_client_writable(
    database_url: str,
) -> None:
    """Prove clients can review only their own server-persisted import history."""
    first_user, second_user = uuid.uuid4(), uuid.uuid4()
    staged_import_id: uuid.UUID
    staged_row_id: uuid.UUID
    staged_file_id: uuid.UUID
    validation_result_id: uuid.UUID
    state_event_id: uuid.UUID
    tables = (
        "staged_imports",
        "staged_import_files",
        "staged_import_rows",
        "staged_import_validation_results",
        "staged_import_state_events",
    )

    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection,
                first_user,
                f"staged-first-{first_user}@example.test",
            )
            _insert_auth_user(
                admin_connection,
                second_user,
                f"staged-second-{second_user}@example.test",
            )

        with psycopg.connect(database_url) as anonymous_connection:
            with anonymous_connection.transaction():
                anonymous_connection.execute("SET LOCAL ROLE anon")
                for table in tables:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with anonymous_connection.transaction():
                            anonymous_connection.execute(
                                f"SELECT id FROM public.{table}"
                            ).fetchall()

        with psycopg.connect(database_url) as admin_connection:
            with admin_connection.transaction():
                staged_import_id = admin_connection.execute(
                    """
                    INSERT INTO public.staged_imports (
                        user_id, source_provider, source_format, trusted_staged_at
                    )
                    VALUES (%s, 'trade_republic', 'csv_v1', timezone('utc', now()))
                    RETURNING id
                    """,
                    (first_user,),
                ).fetchone()[0]
                admin_connection.execute(
                    """
                    INSERT INTO storage.objects (bucket_id, name, owner)
                    VALUES ('raw-imports', %s, %s)
                    """,
                    (f"{first_user}/imports/fixture.csv", first_user),
                )
                staged_file_id = admin_connection.execute(
                    """
                    INSERT INTO public.staged_import_files (
                        user_id, staged_import_id, bucket_id, object_path, filename,
                        content_type, byte_size, sha256
                    )
                    VALUES (%s, %s, 'raw-imports', %s, 'fixture.csv', 'text/csv',
                            42, %s)
                    RETURNING id
                    """,
                    (
                        first_user,
                        staged_import_id,
                        f"{first_user}/imports/fixture.csv",
                        "a" * 64,
                    ),
                ).fetchone()[0]
                staged_row_id = admin_connection.execute(
                    """
                    INSERT INTO public.staged_import_rows (
                        user_id, staged_import_id, source_row_number, source_row,
                        parsed_output
                    )
                    VALUES (%s, %s, 1, '{"Transaction ID":"fixture-1"}',
                            '{"status":"parsed"}')
                    RETURNING id
                    """,
                    (first_user, staged_import_id),
                ).fetchone()[0]
                validation_result_id = admin_connection.execute(
                    """
                    INSERT INTO public.staged_import_validation_results (
                        user_id, staged_import_id, staged_import_row_id, code,
                        severity, message, details
                    )
                    VALUES (%s, %s, %s, 'fixture.notice', 'info',
                            'Synthetic fixture row', '{"source":"test"}')
                    RETURNING id
                    """,
                    (first_user, staged_import_id, staged_row_id),
                ).fetchone()[0]
                state_event_id = admin_connection.execute(
                    """
                    INSERT INTO public.staged_import_state_events (
                        user_id, staged_import_id, position, state
                    )
                    VALUES (%s, %s, 1, 'staged')
                    RETURNING id
                    """,
                    (first_user, staged_import_id),
                ).fetchone()[0]

        with psycopg.connect(database_url) as first_connection:
            with first_connection.transaction():
                _as_authenticated_user(first_connection, first_user)
                for table, expected_id in (
                    ("staged_imports", staged_import_id),
                    ("staged_import_files", staged_file_id),
                    ("staged_import_rows", staged_row_id),
                    ("staged_import_validation_results", validation_result_id),
                    ("staged_import_state_events", state_event_id),
                ):
                    assert first_connection.execute(
                        f"SELECT id FROM public.{table}"
                    ).fetchall() == [(expected_id,)]
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with first_connection.transaction():
                            first_connection.execute(
                                f"UPDATE public.{table} SET user_id = user_id "
                                "WHERE id = %s",
                                (expected_id,),
                            )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with first_connection.transaction():
                        first_connection.execute(
                            """
                            INSERT INTO public.staged_imports (
                                user_id, source_provider, source_format
                            )
                            VALUES (%s, 'trade-republic', 'fabricated')
                            """,
                            (first_user,),
                        )
                for statement, parameters in (
                    (
                        """
                        INSERT INTO public.staged_import_rows (
                            user_id, staged_import_id, source_row_number, source_row,
                            parsed_output
                        )
                        VALUES (%s, %s, 99, '{"Transaction ID":"forged"}',
                                '{"candidates":[]}')
                        """,
                        (first_user, staged_import_id),
                    ),
                    (
                        """
                        INSERT INTO public.staged_import_state_events (
                            user_id, staged_import_id, position, state
                        )
                        VALUES (%s, %s, 2, 'parsed')
                        """,
                        (first_user, staged_import_id),
                    ),
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with first_connection.transaction():
                            first_connection.execute(statement, parameters)
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with first_connection.transaction():
                            first_connection.execute(
                                f"DELETE FROM public.{table} WHERE id = %s",
                                (expected_id,),
                            )

        with psycopg.connect(database_url) as second_connection:
            with second_connection.transaction():
                _as_authenticated_user(second_connection, second_user)
                for table in tables:
                    assert (
                        second_connection.execute(
                            f"SELECT id FROM public.{table}"
                        ).fetchall()
                        == []
                    )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with second_connection.transaction():
                        second_connection.execute(
                            """
                            INSERT INTO public.staged_imports (
                                user_id, source_provider, source_format
                            )
                            VALUES (%s, 'trade_republic', 'csv_v1')
                            """,
                            (first_user,),
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


def test_staged_import_constraints_preserve_private_audit_history(
    database_url: str,
) -> None:
    """Prove file, row, diagnostic, and lifecycle invariants are database-enforced."""
    owner_id = uuid.uuid4()
    staged_import_id: uuid.UUID
    invalid_path_import_id: uuid.UUID

    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection,
                owner_id,
                f"staged-constraints-{owner_id}@example.test",
            )

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                staged_import_id = connection.execute(
                    """
                    INSERT INTO public.staged_imports (
                        user_id, source_provider, source_format
                    )
                    VALUES (%s, 'trade_republic', 'csv_v1')
                    RETURNING id
                    """,
                    (owner_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO storage.objects (bucket_id, name, owner)
                    VALUES ('raw-imports', %s, %s)
                    """,
                    (f"{owner_id}/imports/constraints.csv", owner_id),
                )
                connection.execute(
                    """
                    INSERT INTO public.staged_import_files (
                        user_id, staged_import_id, bucket_id, object_path, filename,
                        content_type, byte_size, sha256
                    )
                    VALUES (%s, %s, 'raw-imports', %s, 'constraints.csv', 'text/csv',
                            10, %s)
                    """,
                    (
                        owner_id,
                        staged_import_id,
                        f"{owner_id}/imports/constraints.csv",
                        "b" * 64,
                    ),
                )
                invalid_path_import_id = connection.execute(
                    """
                    INSERT INTO public.staged_imports (
                        user_id, source_provider, source_format
                    )
                    VALUES (%s, 'trade_republic', 'csv_v1')
                    RETURNING id
                    """,
                    (owner_id,),
                ).fetchone()[0]
                with pytest.raises(psycopg.errors.CheckViolation):
                    with connection.transaction():
                        connection.execute(
                            """
                            INSERT INTO public.staged_import_files (
                                user_id, staged_import_id, bucket_id, object_path,
                                filename, content_type, byte_size, sha256
                            )
                            VALUES (%s, %s, 'raw-imports', %s, 'wrong-owner.csv',
                                    'text/csv', 1, %s)
                            """,
                            (
                                owner_id,
                                invalid_path_import_id,
                                f"{uuid.uuid4()}/imports/wrong-owner.csv",
                                "d" * 64,
                            ),
                        )
                connection.execute(
                    """
                    INSERT INTO public.staged_import_rows (
                        user_id, staged_import_id, source_row_number, source_row
                    )
                    VALUES (%s, %s, 1, '{"Transaction ID":"fixture-1"}')
                    """,
                    (owner_id, staged_import_id),
                )
                connection.execute(
                    """
                    INSERT INTO public.staged_import_state_events (
                        user_id, staged_import_id, position, state
                    )
                    VALUES (%s, %s, 1, 'staged')
                    """,
                    (owner_id, staged_import_id),
                )

                for statement, parameters in (
                    (
                        """
                        INSERT INTO public.staged_import_files (
                            user_id, staged_import_id, bucket_id, object_path,
                            filename, content_type, byte_size, sha256
                        )
                        VALUES (%s, %s, 'not-raw-imports', %s, 'wrong.csv',
                                'text/csv', 1, %s)
                        """,
                        (
                            owner_id,
                            staged_import_id,
                            f"{owner_id}/imports/wrong.csv",
                            "c" * 64,
                        ),
                    ),
                    (
                        """
                        INSERT INTO public.staged_import_files (
                            user_id, staged_import_id, bucket_id, object_path,
                            filename, content_type, byte_size, sha256
                        )
                        VALUES (%s, %s, 'raw-imports', %s, 'duplicate.csv',
                                'text/csv', 1, %s)
                        """,
                        (
                            owner_id,
                            staged_import_id,
                            f"{owner_id}/imports/duplicate.csv",
                            "c" * 64,
                        ),
                    ),
                    (
                        """
                        INSERT INTO public.staged_import_rows (
                            user_id, staged_import_id, source_row_number, source_row
                        )
                        VALUES (%s, %s, 1, '{"Transaction ID":"duplicate"}')
                        """,
                        (owner_id, staged_import_id),
                    ),
                    (
                        """
                        INSERT INTO public.staged_import_rows (
                            user_id, staged_import_id, source_row_number, source_row
                        )
                        VALUES (%s, %s, 0, '{"Transaction ID":"zero"}')
                        """,
                        (owner_id, staged_import_id),
                    ),
                ):
                    with pytest.raises(
                        (psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation)
                    ):
                        with connection.transaction():
                            connection.execute(statement, parameters)

                with pytest.raises(psycopg.errors.RaiseException):
                    with connection.transaction():
                        connection.execute(
                            """
                            INSERT INTO public.staged_import_state_events (
                                user_id, staged_import_id, position, state
                            )
                            VALUES (%s, %s, 1, 'parsed')
                            """,
                            (owner_id, staged_import_id),
                        )

                with pytest.raises(psycopg.errors.RaiseException):
                    with connection.transaction():
                        connection.execute(
                            """
                            INSERT INTO public.staged_import_state_events (
                                user_id, staged_import_id, position, state
                            )
                            VALUES (%s, %s, 2, 'confirmed')
                            """,
                            (owner_id, staged_import_id),
                        )

                connection.execute(
                    """
                    INSERT INTO public.staged_import_state_events (
                        user_id, staged_import_id, position, state
                    )
                    VALUES (%s, %s, 2, 'parsed'), (%s, %s, 3, 'validated'),
                            (%s, %s, 4, 'blocked')
                    """,
                    (
                        owner_id,
                        staged_import_id,
                        owner_id,
                        staged_import_id,
                        owner_id,
                        staged_import_id,
                    ),
                )
                with pytest.raises(psycopg.errors.RaiseException):
                    with connection.transaction():
                        connection.execute(
                            """
                            INSERT INTO public.staged_import_state_events (
                                user_id, staged_import_id, position, state
                            )
                            VALUES (%s, %s, 5, 'parsed')
                            """,
                            (owner_id, staged_import_id),
                        )

                columns = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                            AND table_name = 'staged_import_files'
                        """
                    ).fetchall()
                }
                assert not any(
                    column in {"url", "content", "raw_content"}
                    or column.endswith("_url")
                    for column in columns
                )
                assert connection.execute(
                    """
                    SELECT bucket_id, object_path
                    FROM public.staged_import_files
                    WHERE staged_import_id = %s
                    """,
                    (staged_import_id,),
                ).fetchone() == (
                    "raw-imports",
                    f"{owner_id}/imports/constraints.csv",
                )
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id = %s", (owner_id,)
            )


def test_staged_import_confirmation_is_atomic_owner_scoped_and_idempotent(
    database_url: str,
) -> None:
    """Confirm only one owner's valid import and preserve its durable evidence."""
    owner_id, other_user_id = uuid.uuid4(), uuid.uuid4()
    valid_candidate = """
    {"candidates":[{
        "source_identity":{"provider":"trade-republic","event_reference":"TR-1:base"},
        "event_type":"buy",
        "occurred_at":"2026-07-20T12:00:00+00:00",
        "legs":[
            {"kind":"cash","direction":"out","money":{"amount":"12.3400","currency":"EUR"}},
            {"kind":"instrument","direction":"in","instrument_id":"US0378331005","quantity":{"value":"0.125000"}}
        ]
    }]}"""
    import_id: uuid.UUID

    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection, owner_id, f"confirm-owner-{owner_id}@example.test"
            )
            _insert_auth_user(
                admin_connection,
                other_user_id,
                f"confirm-other-{other_user_id}@example.test",
            )
        with psycopg.connect(database_url) as admin_connection:
            with admin_connection.transaction():
                import_id = _create_review_ready_import(
                    admin_connection, owner_id, valid_candidate
                )

        with psycopg.connect(database_url) as owner_connection:
            with owner_connection.transaction():
                _as_authenticated_user(owner_connection, owner_id)
                owner_connection.execute(
                    "INSERT INTO public.financial_accounts (user_id) VALUES (%s)",
                    (owner_id,),
                )
                assert owner_connection.execute(
                    "SELECT * FROM public.confirm_staged_import(%s)", (import_id,)
                ).fetchone() == (1, False)
                assert owner_connection.execute(
                    """
                    SELECT source_event_reference, staged_import_id
                    FROM public.financial_events
                    WHERE staged_import_id = %s
                    """,
                    (import_id,),
                ).fetchone() == ("TR-1:base", import_id)
                assert owner_connection.execute(
                    "SELECT count(*) FROM public.financial_event_legs"
                ).fetchone() == (2,)
                assert owner_connection.execute(
                    """
                    SELECT state, details FROM public.staged_import_state_events
                    WHERE staged_import_id = %s ORDER BY position DESC LIMIT 1
                    """,
                    (import_id,),
                ).fetchone() == ("confirmed", {"event_count": 1})
                assert owner_connection.execute(
                    """
                    SELECT event_type, metadata FROM public.audit_events
                    WHERE actor_id = %s
                    """,
                    (owner_id,),
                ).fetchone() == (
                    "import.confirmed",
                    {"staged_import_id": str(import_id), "event_count": 1},
                )
                assert owner_connection.execute(
                    "SELECT * FROM public.confirm_staged_import(%s)", (import_id,)
                ).fetchone() == (1, True)
                assert owner_connection.execute(
                    "SELECT count(*) FROM public.audit_events WHERE actor_id = %s",
                    (owner_id,),
                ).fetchone() == (1,)

        with psycopg.connect(database_url) as anonymous_connection:
            with anonymous_connection.transaction():
                anonymous_connection.execute("SET LOCAL ROLE anon")
                with pytest.raises(
                    (
                        psycopg.errors.InsufficientPrivilege,
                        psycopg.errors.InvalidAuthorizationSpecification,
                    )
                ):
                    anonymous_connection.execute(
                        "SELECT * FROM public.confirm_staged_import(%s)", (import_id,)
                    )

        with psycopg.connect(database_url) as other_connection:
            with other_connection.transaction():
                _as_authenticated_user(other_connection, other_user_id)
                with pytest.raises(psycopg.errors.NoDataFound):
                    other_connection.execute(
                        "SELECT * FROM public.confirm_staged_import(%s)", (import_id,)
                    )
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (owner_id, other_user_id),
            )


def test_trusted_parser_staging_can_be_confirmed(database_url: str) -> None:
    """The API's server-only writer produces confirmation-eligible evidence."""
    owner_id = uuid.uuid4()
    content = (
        Path(__file__).parent / "fixtures/trade_republic_csv_v1/accepted-observed.csv"
    ).read_bytes()
    batch = parse_trade_republic_csv(content)
    import_id = str(uuid.uuid4())

    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection, owner_id, f"trusted-stage-{owner_id}@example.test"
            )

        asyncio.run(
            TrustedStagedImportWriter(Settings()).stage(
                user_id=str(owner_id),
                import_id=import_id,
                path=f"{owner_id}/imports/{import_id}.csv",
                filename="accepted-observed.csv",
                content_type="text/csv",
                content=content,
                batch=batch,
            )
        )

        with psycopg.connect(database_url) as owner_connection:
            with owner_connection.transaction():
                _as_authenticated_user(owner_connection, owner_id)
                owner_connection.execute(
                    "INSERT INTO public.financial_accounts (user_id) VALUES (%s)",
                    (owner_id,),
                )
                assert owner_connection.execute(
                    """
                    SELECT trusted_staged_at IS NOT NULL
                    FROM public.staged_imports WHERE id = %s
                    """,
                    (import_id,),
                ).fetchone() == (True,)
                assert owner_connection.execute(
                    "SELECT * FROM public.confirm_staged_import(%s)", (import_id,)
                ).fetchone() == (
                    sum(len(row.candidates) for row in batch.rows),
                    False,
                )
                confirmed_groups = owner_connection.execute(
                    """
                    SELECT source_event_reference, source_group_reference
                    FROM public.financial_events
                    WHERE staged_import_id = %s
                    ORDER BY source_event_reference
                    """,
                    (import_id,),
                ).fetchall()
                assert (
                    "synthetic-tr-buy:base",
                    "synthetic-tr-buy",
                ) in confirmed_groups
                assert (
                    "synthetic-tr-buy:fee",
                    "synthetic-tr-buy",
                ) in confirmed_groups
                assert (
                    "synthetic-tr-dividend:base",
                    "synthetic-tr-dividend",
                ) in confirmed_groups
                assert (
                    "synthetic-tr-dividend:withholding-tax",
                    "synthetic-tr-dividend",
                ) in confirmed_groups
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id = %s", (owner_id,)
            )


def test_staged_import_confirmation_rolls_back_on_invalid_ledger_history(
    database_url: str,
) -> None:
    """A deferred ledger failure must retain review_ready with no partial writes."""
    owner_id = uuid.uuid4()
    invalid_candidate = """
    {"candidates":[{
        "source_identity":{"provider":"trade-republic","event_reference":"TR-broken:base"},
        "event_type":"deposit",
        "occurred_at":"2026-07-20T12:00:00+00:00",
        "legs":[]
    }]}"""
    import_id: uuid.UUID

    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection, owner_id, f"confirm-rollback-{owner_id}@example.test"
            )
        with psycopg.connect(database_url) as admin_connection:
            with admin_connection.transaction():
                import_id = _create_review_ready_import(
                    admin_connection, owner_id, invalid_candidate
                )

        with psycopg.connect(database_url) as owner_connection:
            with owner_connection.transaction():
                _as_authenticated_user(owner_connection, owner_id)
                owner_connection.execute(
                    "INSERT INTO public.financial_accounts (user_id) VALUES (%s)",
                    (owner_id,),
                )
                with pytest.raises(psycopg.errors.RaiseException):
                    with owner_connection.transaction():
                        owner_connection.execute(
                            "SELECT * FROM public.confirm_staged_import(%s)",
                            (import_id,),
                        )
                        owner_connection.execute("SET CONSTRAINTS ALL IMMEDIATE")
                assert owner_connection.execute(
                    "SELECT count(*) FROM public.financial_events "
                    "WHERE staged_import_id = %s",
                    (import_id,),
                ).fetchone() == (0,)
                assert owner_connection.execute(
                    """
                    SELECT state FROM public.staged_import_state_events
                    WHERE staged_import_id = %s ORDER BY position DESC LIMIT 1
                    """,
                    (import_id,),
                ).fetchone() == ("review_ready",)
                assert owner_connection.execute(
                    "SELECT count(*) FROM public.audit_events WHERE actor_id = %s",
                    (owner_id,),
                ).fetchone() == (0,)
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id = %s", (owner_id,)
            )


def test_confirmation_rejects_fabricated_review_ready_import_without_provenance(
    database_url: str,
) -> None:
    """A legacy or fabricated review-ready batch cannot create ledger history."""
    owner_id = uuid.uuid4()
    fabricated_candidate = """
    {"candidates":[{
        "source_identity":{"provider":"trade-republic","event_reference":"forged"},
        "event_type":"deposit",
        "occurred_at":"2026-07-20T12:00:00+00:00",
        "legs":[
            {"kind":"cash","direction":"in","money":{"amount":"12.34","currency":"EUR"}}
        ]
    }]}"""

    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection, owner_id, f"forged-import-{owner_id}@example.test"
            )
        with psycopg.connect(database_url) as admin_connection:
            with admin_connection.transaction():
                import_id = _create_review_ready_import(
                    admin_connection,
                    owner_id,
                    fabricated_candidate,
                    trusted=False,
                )

        with psycopg.connect(database_url) as owner_connection:
            with owner_connection.transaction():
                _as_authenticated_user(owner_connection, owner_id)
                owner_connection.execute(
                    "INSERT INTO public.financial_accounts (user_id) VALUES (%s)",
                    (owner_id,),
                )
                with pytest.raises(psycopg.errors.RaiseException):
                    with owner_connection.transaction():
                        owner_connection.execute(
                            "SELECT * FROM public.confirm_staged_import(%s)",
                            (import_id,),
                        )
                assert owner_connection.execute(
                    "SELECT count(*) FROM public.financial_events "
                    "WHERE staged_import_id = %s",
                    (import_id,),
                ).fetchone() == (0,)
                assert owner_connection.execute(
                    "SELECT count(*) FROM public.audit_events WHERE actor_id = %s",
                    (owner_id,),
                ).fetchone() == (0,)
                assert owner_connection.execute(
                    """
                    SELECT state FROM public.staged_import_state_events
                    WHERE staged_import_id = %s ORDER BY position DESC LIMIT 1
                    """,
                    (import_id,),
                ).fetchone() == ("review_ready",)
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id = %s", (owner_id,)
            )


def test_source_groups_are_owner_scoped_and_immutable(database_url: str) -> None:
    """Group evidence cannot be read or written across an ownership boundary."""
    owner_id, other_user_id = uuid.uuid4(), uuid.uuid4()
    try:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            _insert_auth_user(
                admin_connection, owner_id, f"group-owner-{owner_id}@example.test"
            )
            _insert_auth_user(
                admin_connection,
                other_user_id,
                f"group-other-{other_user_id}@example.test",
            )
        with psycopg.connect(database_url) as admin_connection:
            with admin_connection.transaction():
                owner_account_id = admin_connection.execute(
                    """
                    INSERT INTO public.financial_accounts (user_id)
                    VALUES (%s)
                    RETURNING id
                    """,
                    (owner_id,),
                ).fetchone()[0]

        with psycopg.connect(database_url) as owner_connection:
            with owner_connection.transaction():
                _as_authenticated_user(owner_connection, owner_id)
                event_id = owner_connection.execute(
                    """
                    INSERT INTO public.financial_events (
                        user_id, account_id, source_provider,
                        source_event_reference, source_group_reference,
                        event_type, occurred_at
                    )
                    VALUES (%s, %s, 'fixture', 'grouped-deposit', 'group-1',
                            'deposit', now())
                    RETURNING id
                    """,
                    (owner_id, owner_account_id),
                ).fetchone()[0]
                owner_connection.execute(
                    """
                    INSERT INTO public.financial_event_legs (
                        event_id, user_id, account_id, position, leg_kind,
                        direction, cash_amount, cash_currency
                    )
                    VALUES (%s, %s, %s, 1, 'cash', 'in', 1, 'EUR')
                    """,
                    (event_id, owner_id, owner_account_id),
                )
                assert owner_connection.execute(
                    """
                    SELECT account_id, source_provider, source_group_reference
                    FROM public.financial_source_event_groups
                    """
                ).fetchall() == [(owner_account_id, "fixture", "group-1")]

        with psycopg.connect(database_url) as anonymous_connection:
            with anonymous_connection.transaction():
                anonymous_connection.execute("SET LOCAL ROLE anon")
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    anonymous_connection.execute(
                        "SELECT * FROM public.financial_source_event_groups"
                    ).fetchall()

        with psycopg.connect(database_url) as other_connection:
            with other_connection.transaction():
                _as_authenticated_user(other_connection, other_user_id)
                assert (
                    other_connection.execute(
                        "SELECT * FROM public.financial_source_event_groups"
                    ).fetchall()
                    == []
                )
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    other_connection.execute(
                        """
                        INSERT INTO public.financial_source_event_groups (
                            user_id, account_id, source_provider,
                            source_group_reference
                        )
                        VALUES (%s, %s, 'fixture', 'group-1')
                        """,
                        (owner_id, owner_account_id),
                    )
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin_connection:
            admin_connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (owner_id, other_user_id),
            )


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
                    (None, FIXTURE_HISTORY[-2].event_id),
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
