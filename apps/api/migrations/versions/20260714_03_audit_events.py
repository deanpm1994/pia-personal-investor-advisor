"""Add append-only security audit events.

Revision ID: 20260714_03
Revises: 20260714_02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260714_03"
down_revision: str | Sequence[str] | None = "20260714_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.audit_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            actor_id uuid NOT NULL,
            event_type text NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute("ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON public.audit_events FROM PUBLIC, anon, authenticated")
    op.execute("GRANT SELECT ON public.audit_events TO authenticated")
    op.execute(
        """
        CREATE POLICY audit_events_select_own ON public.audit_events
        FOR SELECT TO authenticated
        USING ((SELECT auth.uid()) = actor_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE public.audit_events")
