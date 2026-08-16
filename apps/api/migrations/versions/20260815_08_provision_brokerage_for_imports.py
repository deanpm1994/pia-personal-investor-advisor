"""Provision one default brokerage account when confirming a first import.

Revision ID: 20260815_08
Revises: 20260815_07
Create Date: 2026-08-15 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260815_08"
down_revision: str | Sequence[str] | None = "20260815_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ACCOUNT_SELECTION = """            SELECT count(*)
            INTO v_account_count
            FROM public.financial_accounts
            WHERE user_id = v_user_id
                AND role = 'brokerage'
                AND archived_at IS NULL;"""

_ACCOUNT_PROVISION = """            INSERT INTO public.financial_accounts (user_id)
            VALUES (v_user_id)
            ON CONFLICT (user_id) WHERE role = 'brokerage' AND archived_at IS NULL
            DO NOTHING;
"""


def _replace_confirmation_function(*, downgrade: bool = False) -> None:
    """Insert account provisioning before the existing account selection."""
    needle = (
        _ACCOUNT_PROVISION + _ACCOUNT_SELECTION if downgrade else _ACCOUNT_SELECTION
    )
    replacement = (
        _ACCOUNT_SELECTION if downgrade else _ACCOUNT_PROVISION + _ACCOUNT_SELECTION
    )
    op.execute(
        f"""
        DO $$
        DECLARE
            function_sql text;
        BEGIN
            SELECT pg_get_functiondef(p.oid)
            INTO function_sql
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
                AND p.proname = 'confirm_staged_import'
                AND pg_get_function_identity_arguments(p.oid)
                    = 'p_staged_import_id uuid';

            IF function_sql IS NULL
                OR position($needle${needle}$needle$ IN function_sql) = 0 THEN
                RAISE EXCEPTION
                    'confirm_staged_import(uuid) has an unexpected account selection';
            END IF;

            function_sql := replace(
                function_sql,
                $needle${needle}$needle$,
                $replacement${replacement}$replacement$
            );
            EXECUTE function_sql;
        END;
        $$
        """
    )


def upgrade() -> None:
    """Make a first CSV import usable without a separate account setup screen."""
    _replace_confirmation_function()


def downgrade() -> None:
    """Restore the explicit-account requirement for earlier revisions."""
    _replace_confirmation_function(downgrade=True)
