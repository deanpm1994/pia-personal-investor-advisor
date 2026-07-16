"""Add the owner-scoped, append-only canonical financial ledger.

Revision ID: 20260716_04
Revises: 20260714_03
Create Date: 2026-07-16 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260716_04"
down_revision: str | Sequence[str] | None = "20260714_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create PIA-owned ledger tables, constraints, and client RLS policies."""
    op.execute(
        """
        CREATE TABLE public.financial_accounts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            UNIQUE (id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.financial_instruments (
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            instrument_id text NOT NULL CHECK (btrim(instrument_id) <> ''),
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            PRIMARY KEY (user_id, instrument_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.financial_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            account_id uuid NOT NULL,
            source_provider text NOT NULL CHECK (btrim(source_provider) <> ''),
            source_event_reference text NOT NULL
                CHECK (btrim(source_event_reference) <> ''),
            event_type text NOT NULL CHECK (event_type IN (
                'deposit', 'withdrawal', 'buy', 'sell', 'dividend', 'interest',
                'fee', 'withholding_tax', 'source_reported_fx_conversion',
                'stock_split', 'correction', 'reversal'
            )),
            occurred_at timestamptz NOT NULL,
            source_reported_eur_amount numeric,
            source_reported_eur_rate numeric,
            source_reported_eur_reported_at timestamptz,
            correction_of_event_id uuid,
            reversal_of_event_id uuid,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            FOREIGN KEY (account_id, user_id)
                REFERENCES public.financial_accounts (id, user_id) ON DELETE CASCADE,
            UNIQUE (id, user_id, account_id),
            UNIQUE (user_id, account_id, source_provider, source_event_reference),
            FOREIGN KEY (correction_of_event_id, user_id, account_id)
                REFERENCES public.financial_events (id, user_id, account_id)
                DEFERRABLE INITIALLY DEFERRED,
            FOREIGN KEY (reversal_of_event_id, user_id, account_id)
                REFERENCES public.financial_events (id, user_id, account_id)
                DEFERRABLE INITIALLY DEFERRED,
            CHECK (
                (source_reported_eur_amount IS NULL
                    AND source_reported_eur_rate IS NULL
                    AND source_reported_eur_reported_at IS NULL)
                OR (
                    source_reported_eur_amount > 0
                    AND source_reported_eur_rate > 0
                    AND source_reported_eur_reported_at IS NOT NULL
                )
            ),
            CHECK (
                (event_type = 'correction'
                    AND correction_of_event_id IS NOT NULL
                    AND reversal_of_event_id IS NULL)
                OR (event_type = 'reversal'
                    AND reversal_of_event_id IS NOT NULL
                    AND correction_of_event_id IS NULL)
                OR (event_type NOT IN ('correction', 'reversal')
                    AND correction_of_event_id IS NULL
                    AND reversal_of_event_id IS NULL)
            ),
            CHECK (correction_of_event_id IS NULL OR correction_of_event_id <> id),
            CHECK (reversal_of_event_id IS NULL OR reversal_of_event_id <> id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.financial_event_legs (
            event_id uuid NOT NULL,
            user_id uuid NOT NULL,
            account_id uuid NOT NULL,
            position integer NOT NULL CHECK (position > 0),
            leg_kind text NOT NULL CHECK (leg_kind IN ('cash', 'instrument')),
            direction text NOT NULL CHECK (direction IN ('in', 'out')),
            cash_amount numeric,
            cash_currency text,
            instrument_id text,
            quantity numeric,
            PRIMARY KEY (event_id, position),
            FOREIGN KEY (user_id)
                REFERENCES public.profiles (id) ON DELETE CASCADE,
            FOREIGN KEY (event_id, user_id, account_id)
                REFERENCES public.financial_events (id, user_id, account_id)
                ON DELETE CASCADE,
            FOREIGN KEY (user_id, instrument_id)
                REFERENCES public.financial_instruments (user_id, instrument_id)
                ON DELETE CASCADE,
            CHECK (
                (leg_kind = 'cash'
                    AND cash_amount > 0
                    AND cash_currency ~ '^[A-Z]{3}$'
                    AND instrument_id IS NULL
                    AND quantity IS NULL)
                OR (leg_kind = 'instrument'
                    AND instrument_id IS NOT NULL
                    AND btrim(instrument_id) <> ''
                    AND quantity > 0
                    AND cash_amount IS NULL
                    AND cash_currency IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.enforce_financial_event_shape()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            target_event_id uuid;
            current_event_type text;
            leg_count integer;
            cash_count integer;
            instrument_count integer;
            inbound_count integer;
            outbound_count integer;
            cash_currency_count integer;
            instrument_id_count integer;
        BEGIN
            IF TG_TABLE_NAME = 'financial_events' THEN
                target_event_id := NEW.id;
            ELSE
                target_event_id := NEW.event_id;
            END IF;

            SELECT event_type INTO current_event_type
            FROM public.financial_events
            WHERE id = target_event_id;

            IF NOT FOUND THEN
                RETURN NULL;
            END IF;

            SELECT
                count(*),
                count(*) FILTER (WHERE leg_kind = 'cash'),
                count(*) FILTER (WHERE leg_kind = 'instrument'),
                count(*) FILTER (WHERE direction = 'in'),
                count(*) FILTER (WHERE direction = 'out'),
                count(DISTINCT cash_currency) FILTER (WHERE leg_kind = 'cash'),
                count(DISTINCT instrument_id) FILTER (WHERE leg_kind = 'instrument')
            INTO leg_count, cash_count, instrument_count, inbound_count, outbound_count,
                cash_currency_count, instrument_id_count
            FROM public.financial_event_legs
            WHERE event_id = target_event_id;

            IF current_event_type IN ('deposit', 'dividend', 'interest')
                AND NOT (leg_count = 1 AND cash_count = 1 AND inbound_count = 1) THEN
                RAISE EXCEPTION 'financial event % requires one inbound cash leg',
                    target_event_id;
            ELSIF current_event_type IN ('withdrawal', 'fee', 'withholding_tax')
                AND NOT (leg_count = 1 AND cash_count = 1 AND outbound_count = 1) THEN
                RAISE EXCEPTION 'financial event % requires one outbound cash leg',
                    target_event_id;
            ELSIF current_event_type = 'buy'
                AND NOT (
                    leg_count = 2 AND cash_count = 1 AND instrument_count = 1
                    AND inbound_count = 1 AND outbound_count = 1
                    AND EXISTS (
                        SELECT 1 FROM public.financial_event_legs
                        WHERE event_id = target_event_id
                            AND leg_kind = 'cash' AND direction = 'out'
                    )
                    AND EXISTS (
                        SELECT 1 FROM public.financial_event_legs
                        WHERE event_id = target_event_id
                            AND leg_kind = 'instrument' AND direction = 'in'
                    )
                ) THEN
                RAISE EXCEPTION 'financial event % has an invalid buy leg shape',
                    target_event_id;
            ELSIF current_event_type = 'sell'
                AND NOT (
                    leg_count = 2 AND cash_count = 1 AND instrument_count = 1
                    AND inbound_count = 1 AND outbound_count = 1
                    AND EXISTS (
                        SELECT 1 FROM public.financial_event_legs
                        WHERE event_id = target_event_id
                            AND leg_kind = 'cash' AND direction = 'in'
                    )
                    AND EXISTS (
                        SELECT 1 FROM public.financial_event_legs
                        WHERE event_id = target_event_id
                            AND leg_kind = 'instrument' AND direction = 'out'
                    )
                ) THEN
                RAISE EXCEPTION 'financial event % has an invalid sell leg shape',
                    target_event_id;
            ELSIF current_event_type = 'source_reported_fx_conversion'
                AND NOT (
                    leg_count = 2 AND cash_count = 2 AND inbound_count = 1
                    AND outbound_count = 1 AND cash_currency_count = 2
                ) THEN
                RAISE EXCEPTION
                    'financial event % requires opposing cash legs in two currencies',
                    target_event_id;
            ELSIF current_event_type = 'stock_split'
                AND NOT (
                    leg_count = 2 AND instrument_count = 2 AND inbound_count = 1
                    AND outbound_count = 1 AND instrument_id_count = 1
                ) THEN
                RAISE EXCEPTION
                    'financial event % requires opposing legs for one instrument',
                    target_event_id;
            ELSIF current_event_type IN ('correction', 'reversal')
                AND leg_count < 1 THEN
                RAISE EXCEPTION 'financial event % requires at least one leg',
                    target_event_id;
            END IF;

            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER financial_events_shape_check
        AFTER INSERT OR UPDATE ON public.financial_events
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.enforce_financial_event_shape()
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER financial_event_legs_shape_check
        AFTER INSERT OR UPDATE ON public.financial_event_legs
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION public.enforce_financial_event_shape()
        """
    )

    for table in (
        "financial_accounts",
        "financial_instruments",
        "financial_events",
        "financial_event_legs",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT, INSERT ON public.{table} TO authenticated")
        op.execute(
            f"""
            CREATE POLICY {table}_select_own ON public.{table}
            FOR SELECT TO authenticated
            USING ((SELECT auth.uid()) = user_id)
            """
        )
        op.execute(
            f"""
            CREATE POLICY {table}_insert_own ON public.{table}
            FOR INSERT TO authenticated
            WITH CHECK ((SELECT auth.uid()) = user_id)
            """
        )


def downgrade() -> None:
    """Remove the P3.3 application-owned ledger objects."""
    op.execute("DROP TABLE public.financial_event_legs")
    op.execute("DROP TABLE public.financial_events")
    op.execute("DROP TABLE public.financial_instruments")
    op.execute("DROP TABLE public.financial_accounts")
    op.execute("DROP FUNCTION public.enforce_financial_event_shape()")
