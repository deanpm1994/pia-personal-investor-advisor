"""Add founder attestation, quota, and scheduled EOD run audit state.

Revision ID: 20260826_13
Revises: 20260825_12
Create Date: 2026-08-26 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260826_13"
down_revision: str | Sequence[str] | None = "20260825_12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist server-controlled enablement, quota, and schedule evidence."""
    op.execute(
        """
        ALTER TABLE public.market_provider_access
            ADD COLUMN risk_attestation_version text,
            ADD COLUMN risk_attested_at timestamptz,
            ADD COLUMN risk_withdrawn_at timestamptz,
            ADD CONSTRAINT market_provider_access_risk_gate_check CHECK (
                provider <> 'marketstack'
                OR access_status <> 'enabled'
                OR (
                    risk_attestation_version = 'adr-0009-founder-risk-v1'
                    AND risk_attested_at IS NOT NULL
                    AND risk_withdrawn_at IS NULL
                )
            ),
            ADD CONSTRAINT market_provider_access_withdrawal_check CHECK (
                risk_withdrawn_at IS NULL
                OR (
                    risk_attested_at IS NOT NULL
                    AND risk_withdrawn_at >= risk_attested_at
                )
            )
        """
    )
    op.execute(
        """
        ALTER TABLE public.market_ingestion_runs
        DROP CONSTRAINT market_ingestion_runs_status_check,
        ADD CONSTRAINT market_ingestion_runs_status_check CHECK (
            status IN (
                'started', 'completed', 'partial', 'failed',
                'provider_disabled', 'license_review_required',
                'quota_exhausted'
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.market_quota_usage (
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            provider text NOT NULL CHECK (
                provider ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
            ),
            month_start date NOT NULL CHECK (
                month_start = date_trunc('month', month_start)::date
            ),
            routine_requests integer NOT NULL DEFAULT 0 CHECK (
                routine_requests BETWEEN 0 AND 72
            ),
            reserve_requests integer NOT NULL DEFAULT 0 CHECK (
                reserve_requests BETWEEN 0 AND 100
            ),
            updated_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            PRIMARY KEY (user_id, provider, month_start),
            CHECK (routine_requests + reserve_requests <= 100)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.market_schedule_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            provider text NOT NULL CHECK (
                provider ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
            ),
            scheduled_for timestamptz NOT NULL,
            target_date date,
            status text NOT NULL CHECK (
                status IN (
                    'skipped', 'completed', 'partial', 'failed',
                    'provider_disabled', 'license_review_required',
                    'quota_exhausted'
                )
            ),
            eligible_instruments integer NOT NULL CHECK (
                eligible_instruments BETWEEN 0 AND 4
            ),
            fetched_instruments integer NOT NULL CHECK (
                fetched_instruments BETWEEN 0 AND 3
            ),
            successful_instruments integer NOT NULL CHECK (
                successful_instruments BETWEEN 0 AND fetched_instruments
            ),
            diagnostics jsonb NOT NULL CHECK (jsonb_typeof(diagnostics) = 'array'),
            started_at timestamptz NOT NULL,
            finished_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            UNIQUE (id, user_id),
            CHECK (finished_at >= started_at)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX market_schedule_runs_owner_scheduled_idx
        ON public.market_schedule_runs (user_id, scheduled_for DESC, id DESC)
        """
    )
    for table in ("market_quota_usage", "market_schedule_runs"):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON public.{table} TO authenticated")
        op.execute(
            f"""
            CREATE POLICY {table}_select_own ON public.{table}
            FOR SELECT TO authenticated
            USING ((SELECT auth.uid()) = user_id)
            """
        )


def downgrade() -> None:
    """Remove scheduling state and restore the prior provider access shape."""
    op.execute("DROP TABLE public.market_schedule_runs")
    op.execute("DROP TABLE public.market_quota_usage")
    op.execute(
        """
        ALTER TABLE public.market_ingestion_runs
        DROP CONSTRAINT market_ingestion_runs_status_check,
        ADD CONSTRAINT market_ingestion_runs_status_check CHECK (
            status IN (
                'started', 'completed', 'partial', 'failed',
                'provider_disabled', 'license_review_required'
            )
        )
        """
    )
    op.execute(
        """
        ALTER TABLE public.market_provider_access
            DROP CONSTRAINT market_provider_access_withdrawal_check,
            DROP CONSTRAINT market_provider_access_risk_gate_check,
            DROP COLUMN risk_withdrawn_at,
            DROP COLUMN risk_attested_at,
            DROP COLUMN risk_attestation_version
        """
    )
