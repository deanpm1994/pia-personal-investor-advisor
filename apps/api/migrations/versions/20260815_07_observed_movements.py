"""Persist source-faithful movements whose basis or settlement is unavailable.

Revision ID: 20260815_07
Revises: 20260807_10
Create Date: 2026-08-15 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260815_07"
down_revision: str | Sequence[str] | None = "20260807_10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow validated observed cash and instrument movements in the ledger."""
    op.execute(
        "ALTER TABLE public.financial_events "
        "DROP CONSTRAINT financial_events_event_type_check"
    )
    op.execute(
        """
        ALTER TABLE public.financial_events
        ADD CONSTRAINT financial_events_event_type_check CHECK (event_type IN (
            'deposit', 'withdrawal', 'buy', 'sell', 'dividend', 'interest',
            'fee', 'withholding_tax', 'source_reported_fx_conversion',
            'stock_split', 'correction', 'reversal',
            'observed_position_movement', 'observed_cash_movement'
        ))
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.enforce_observed_financial_event_shape()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE event_type_value text; leg_count integer; cash_count integer;
            instrument_count integer;
        BEGIN
            SELECT event_type INTO event_type_value
            FROM public.financial_events WHERE id = NEW.event_id;
            SELECT count(*), count(*) FILTER (WHERE leg_kind = 'cash'),
                count(*) FILTER (WHERE leg_kind = 'instrument')
            INTO leg_count, cash_count, instrument_count
            FROM public.financial_event_legs WHERE event_id = NEW.event_id;
            IF event_type_value = 'observed_position_movement'
                AND NOT (leg_count = 1 AND instrument_count = 1) THEN
                RAISE EXCEPTION
                    'observed position movement requires one instrument leg';
            ELSIF event_type_value = 'observed_cash_movement'
                AND NOT (leg_count = 1 AND cash_count = 1) THEN
                RAISE EXCEPTION 'observed cash movement requires one cash leg';
            END IF;
            RETURN NULL;
        END; $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER observed_financial_event_shape_check
        AFTER INSERT OR UPDATE ON public.financial_event_legs
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION public.enforce_observed_financial_event_shape()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER observed_financial_event_shape_check "
        "ON public.financial_event_legs"
    )
    op.execute("DROP FUNCTION public.enforce_observed_financial_event_shape()")
    op.execute(
        "ALTER TABLE public.financial_events "
        "DROP CONSTRAINT financial_events_event_type_check"
    )
    op.execute(
        "ALTER TABLE public.financial_events "
        "ADD CONSTRAINT financial_events_event_type_check "
        "CHECK (event_type NOT IN "
        "('observed_position_movement', 'observed_cash_movement'))"
    )
