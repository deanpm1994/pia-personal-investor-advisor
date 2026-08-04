"""Add private manual-account metadata and idempotent ledger workflow state.

Revision ID: 20260804_09
Revises: 20260803_08
Create Date: 2026-08-04 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260804_09"
down_revision: str | Sequence[str] | None = "20260803_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _select_active_brokerage_for_confirmation(*, downgrade: bool = False) -> None:
    """Keep import confirmation compatible with additional manual accounts."""
    replacement = "WHERE user_id = v_user_id;"
    if not downgrade:
        replacement = """WHERE user_id = v_user_id
                AND role = 'brokerage'
                AND archived_at IS NULL;"""
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

            IF function_sql IS NULL THEN
                RAISE EXCEPTION 'confirm_staged_import(uuid) is missing';
            END IF;

            function_sql := regexp_replace(
                function_sql,
                E'WHERE user_id = v_user_id\\n[[:space:]]*'
                || E'(AND role = ''brokerage''\\n[[:space:]]*)?'
                || E'AND archived_at IS NULL;',
                'WHERE user_id = v_user_id;',
                'g'
            );
            function_sql := replace(
                function_sql,
                'WHERE user_id = v_user_id;',
                $replacement${replacement}$replacement$
            );
            EXECUTE function_sql;
        END;
        $$
        """
    )


def upgrade() -> None:
    """Persist account metadata without altering existing economic facts."""
    op.execute(
        """
        ALTER TABLE public.financial_accounts
            ADD COLUMN name text NOT NULL DEFAULT 'Brokerage account'
                CHECK (btrim(name) <> ''),
            ADD COLUMN role text NOT NULL DEFAULT 'brokerage'
                CHECK (role IN ('brokerage', 'cash', 'savings', 'emergency_reserve')),
            ADD COLUMN archived_at timestamptz,
            ADD COLUMN emergency_reserve_target_eur numeric,
            ADD COLUMN updated_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            ADD CONSTRAINT financial_accounts_reserve_target_check CHECK (
                emergency_reserve_target_eur IS NULL
                OR (
                    role = 'emergency_reserve'
                    AND emergency_reserve_target_eur > 0
                )
            )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX financial_accounts_one_active_brokerage_per_owner
        ON public.financial_accounts (user_id)
        WHERE role = 'brokerage' AND archived_at IS NULL
        """
    )
    # P5.3's deterministic backfill can leave its deferred ledger checks pending
    # until the surrounding Alembic transaction commits. Resolve them before this
    # migration changes the financial-events account foreign key.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute(
        """
        ALTER TABLE public.financial_events
            DROP CONSTRAINT financial_events_account_id_user_id_fkey,
            ADD CONSTRAINT financial_events_account_id_user_id_fkey
                FOREIGN KEY (account_id, user_id)
                REFERENCES public.financial_accounts (id, user_id) ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        CREATE TABLE public.manual_ledger_idempotency_keys (
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
            operation_id uuid NOT NULL DEFAULT gen_random_uuid(),
            operation_kind text NOT NULL CHECK (btrim(operation_kind) <> ''),
            request_fingerprint text NOT NULL CHECK (
                request_fingerprint ~ '^[0-9a-f]{64}$'
            ),
            result jsonb,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            PRIMARY KEY (user_id, idempotency_key),
            UNIQUE (operation_id),
            CHECK (
                result IS NULL
                OR (
                    jsonb_typeof(result) = 'object'
                    AND result ? 'event_ids'
                    AND jsonb_typeof(result -> 'event_ids') = 'array'
                )
            )
        )
        """
    )
    op.execute(
        "ALTER TABLE public.manual_ledger_idempotency_keys ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "REVOKE ALL ON public.manual_ledger_idempotency_keys "
        "FROM PUBLIC, anon, authenticated"
    )
    _select_active_brokerage_for_confirmation()


def downgrade() -> None:
    """Remove manual-account workflow state without deleting financial facts."""
    _select_active_brokerage_for_confirmation(downgrade=True)
    op.execute("DROP TABLE public.manual_ledger_idempotency_keys")
    op.execute(
        """
        ALTER TABLE public.financial_events
            DROP CONSTRAINT financial_events_account_id_user_id_fkey,
            ADD CONSTRAINT financial_events_account_id_user_id_fkey
                FOREIGN KEY (account_id, user_id)
                REFERENCES public.financial_accounts (id, user_id) ON DELETE CASCADE
        """
    )
    op.execute("DROP INDEX public.financial_accounts_one_active_brokerage_per_owner")
    op.execute(
        """
        ALTER TABLE public.financial_accounts
            DROP CONSTRAINT financial_accounts_reserve_target_check,
            DROP COLUMN updated_at,
            DROP COLUMN emergency_reserve_target_eur,
            DROP COLUMN archived_at,
            DROP COLUMN role,
            DROP COLUMN name
        """
    )
