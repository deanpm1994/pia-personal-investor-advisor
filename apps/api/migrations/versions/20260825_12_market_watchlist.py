"""Persist owner-scoped private market watchlist membership.

Revision ID: 20260825_12
Revises: 20260823_11
Create Date: 2026-08-25 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260825_12"
down_revision: str | Sequence[str] | None = "20260823_11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create private membership without granting browser write access."""
    op.execute(
        """
        CREATE TABLE public.market_watchlist_entries (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            instrument_id uuid NOT NULL,
            added_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            FOREIGN KEY (instrument_id, user_id)
                REFERENCES public.market_instruments (id, user_id)
                ON DELETE CASCADE,
            UNIQUE (id, user_id),
            UNIQUE (user_id, instrument_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX market_watchlist_entries_owner_added_idx
        ON public.market_watchlist_entries (user_id, added_at, id)
        """
    )
    op.execute("ALTER TABLE public.market_watchlist_entries ENABLE ROW LEVEL SECURITY")
    op.execute(
        "REVOKE ALL ON public.market_watchlist_entries FROM PUBLIC, anon, authenticated"
    )
    op.execute("GRANT SELECT ON public.market_watchlist_entries TO authenticated")
    op.execute(
        """
        CREATE POLICY market_watchlist_entries_select_own
        ON public.market_watchlist_entries
        FOR SELECT TO authenticated
        USING ((SELECT auth.uid()) = user_id)
        """
    )


def downgrade() -> None:
    """Remove watchlist membership without altering resolved market identity."""
    op.execute("DROP TABLE public.market_watchlist_entries")
