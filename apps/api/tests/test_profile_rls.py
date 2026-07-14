"""Local-Supabase integration checks for the P2.3 ownership baseline."""

import os
import uuid

import psycopg
import pytest

from pia_api.core.config import Settings

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
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (first_user, second_user),
            )


def test_profile_migration_downgrades_and_upgrades(database_url: str) -> None:
    """Exercise the approved migration's rollback and repeatable upgrade path."""
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    command.downgrade(config, "20260713_01")
    command.upgrade(config, "head")
