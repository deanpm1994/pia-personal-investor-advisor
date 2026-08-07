"""Persist immutable, owner-scoped financial accounting snapshots.

Revision ID: 20260807_10
Revises: 20260804_09
Create Date: 2026-08-07 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260807_10"
down_revision: str | Sequence[str] | None = "20260804_09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create append-only snapshot records with client read-only RLS."""
    op.execute(
        """
        CREATE TABLE public.financial_snapshots (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            status text NOT NULL DEFAULT 'completed'
                CHECK (status = 'completed'),
            input_fingerprint text NOT NULL
                CHECK (input_fingerprint ~ '^[0-9a-f]{64}$'),
            as_of timestamptz,
            refreshed_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            input_watermark jsonb NOT NULL
                CHECK (jsonb_typeof(input_watermark) = 'object'),
            input_counts jsonb NOT NULL
                CHECK (jsonb_typeof(input_counts) = 'object'),
            content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            UNIQUE (user_id, input_fingerprint)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX financial_snapshots_owner_refreshed_idx
        ON public.financial_snapshots (user_id, refreshed_at DESC, id DESC)
        """
    )
    op.execute("ALTER TABLE public.financial_snapshots ENABLE ROW LEVEL SECURITY")
    op.execute(
        "REVOKE ALL ON public.financial_snapshots FROM PUBLIC, anon, authenticated"
    )
    op.execute("GRANT SELECT ON public.financial_snapshots TO authenticated")
    op.execute(
        """
        CREATE POLICY financial_snapshots_select_own
        ON public.financial_snapshots
        FOR SELECT TO authenticated
        USING ((SELECT auth.uid()) = user_id)
        """
    )


def downgrade() -> None:
    """Remove the derived snapshot cache without altering immutable ledger facts."""
    op.execute("DROP TABLE public.financial_snapshots")
