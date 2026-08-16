"""Persist explicit, owner-scoped source groups for ledger attribution.

Revision ID: 20260803_08
Revises: 20260728_07
Create Date: 2026-08-03 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260803_08"
down_revision: str | Sequence[str] | None = "20260728_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_confirmation_function(*, include_source_group: bool) -> None:
    """Keep confirmation atomic while accepting the versioned staged contract."""
    group_column = ", source_group_reference" if include_source_group else ""
    group_value = (
        ", NULLIF(v_candidate ->> 'source_group_reference', '')"
        if include_source_group
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.confirm_staged_import(
            p_staged_import_id uuid
        )
        RETURNS TABLE (event_count integer, already_confirmed boolean)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_user_id uuid := auth.uid();
            v_state text;
            v_trusted_staged_at timestamptz;
            v_account_id uuid;
            v_account_count integer;
            v_row jsonb;
            v_candidate jsonb;
            v_leg jsonb;
            v_event_id uuid;
            v_event_count integer := 0;
            v_next_position integer;
        BEGIN
            IF v_user_id IS NULL THEN
                RAISE EXCEPTION 'authentication required' USING ERRCODE = '28000';
            END IF;

            SELECT trusted_staged_at
            INTO v_trusted_staged_at
            FROM public.staged_imports
            WHERE id = p_staged_import_id AND user_id = v_user_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'staged import not found' USING ERRCODE = 'P0002';
            END IF;

            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_staged_import_id::text, 0)
            );

            SELECT state
            INTO v_state
            FROM public.staged_import_state_events
            WHERE staged_import_id = p_staged_import_id
            ORDER BY position DESC
            LIMIT 1;

            IF v_state = 'confirmed' THEN
                RETURN QUERY
                SELECT count(*)::integer, true
                FROM public.financial_events
                WHERE staged_import_id = p_staged_import_id;
                RETURN;
            END IF;

            IF v_trusted_staged_at IS NULL OR v_state <> 'review_ready' THEN
                RAISE EXCEPTION 'staged import is not ready for confirmation'
                    USING ERRCODE = 'P0001';
            END IF;

            SELECT count(*)
            INTO v_account_count
            FROM public.financial_accounts
            WHERE user_id = v_user_id;
            IF v_account_count <> 1 THEN
                RAISE EXCEPTION 'owner must have exactly one financial account'
                    USING ERRCODE = 'P0001';
            END IF;
            SELECT id
            INTO v_account_id
            FROM public.financial_accounts
            WHERE user_id = v_user_id;

            FOR v_row IN
                SELECT parsed_output
                FROM public.staged_import_rows
                WHERE staged_import_id = p_staged_import_id
                ORDER BY source_row_number
            LOOP
                FOR v_candidate IN
                    SELECT value
                    FROM jsonb_array_elements(
                        coalesce(v_row -> 'candidates', '[]'::jsonb)
                    )
                LOOP
                    INSERT INTO public.financial_events (
                        user_id, account_id, staged_import_id, source_provider,
                        source_event_reference, event_type, occurred_at,
                        source_reported_eur_amount, source_reported_eur_rate,
                        source_reported_eur_reported_at{group_column}
                    )
                    VALUES (
                        v_user_id,
                        v_account_id,
                        p_staged_import_id,
                        v_candidate #>> '{{source_identity,provider}}',
                        v_candidate #>> '{{source_identity,event_reference}}',
                        v_candidate ->> 'event_type',
                        (v_candidate ->> 'occurred_at')::timestamptz,
                        NULLIF(
                            v_candidate #>> '{{source_reported_eur,eur_amount,amount}}',
                            ''
                        )::numeric,
                        NULLIF(
                            v_candidate #>> '{{source_reported_eur,source_rate}}', ''
                        )::numeric,
                        NULLIF(
                            v_candidate #>> '{{source_reported_eur,reported_at}}', ''
                        )::timestamptz{group_value}
                    )
                    RETURNING id INTO v_event_id;

                    FOR v_leg IN
                        SELECT value FROM jsonb_array_elements(v_candidate -> 'legs')
                    LOOP
                        IF v_leg ->> 'kind' = 'instrument' THEN
                            INSERT INTO public.financial_instruments (
                                user_id, instrument_id
                            )
                            VALUES (v_user_id, v_leg ->> 'instrument_id')
                            ON CONFLICT (user_id, instrument_id) DO NOTHING;
                        END IF;
                    END LOOP;

                    INSERT INTO public.financial_event_legs (
                        event_id, user_id, account_id, position, leg_kind, direction,
                        cash_amount, cash_currency, instrument_id, quantity
                    )
                    SELECT
                        v_event_id,
                        v_user_id,
                        v_account_id,
                        ordinality::integer,
                        value ->> 'kind',
                        value ->> 'direction',
                        CASE WHEN value ->> 'kind' = 'cash'
                            THEN (value #>> '{{money,amount}}')::numeric END,
                        CASE WHEN value ->> 'kind' = 'cash'
                            THEN value #>> '{{money,currency}}' END,
                        CASE WHEN value ->> 'kind' = 'instrument'
                            THEN value ->> 'instrument_id' END,
                        CASE WHEN value ->> 'kind' = 'instrument'
                            THEN (value #>> '{{quantity,value}}')::numeric END
                    FROM jsonb_array_elements(v_candidate -> 'legs') WITH ORDINALITY;

                    v_event_count := v_event_count + 1;
                END LOOP;
            END LOOP;

            SELECT coalesce(max(position), 0) + 1
            INTO v_next_position
            FROM public.staged_import_state_events
            WHERE staged_import_id = p_staged_import_id;

            INSERT INTO public.staged_import_state_events (
                user_id, staged_import_id, position, state, details
            )
            VALUES (
                v_user_id,
                p_staged_import_id,
                v_next_position,
                'confirmed',
                jsonb_build_object('event_count', v_event_count)
            );
            INSERT INTO public.audit_events (actor_id, event_type, metadata)
            VALUES (
                v_user_id,
                'import.confirmed',
                jsonb_build_object(
                    'staged_import_id', p_staged_import_id,
                    'event_count', v_event_count
                )
            );

            RETURN QUERY SELECT v_event_count, false;
        END;
        $$
        """
    )


def upgrade() -> None:
    """Add source-group evidence without changing source event identities."""
    op.execute(
        """
        CREATE TABLE public.financial_source_event_groups (
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            account_id uuid NOT NULL,
            source_provider text NOT NULL CHECK (btrim(source_provider) <> ''),
            source_group_reference text NOT NULL
                CHECK (btrim(source_group_reference) <> ''),
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            PRIMARY KEY (
                user_id, account_id, source_provider, source_group_reference
            ),
            FOREIGN KEY (account_id, user_id)
                REFERENCES public.financial_accounts (id, user_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        ALTER TABLE public.financial_events
            ADD COLUMN source_group_reference text
                CHECK (
                    source_group_reference IS NULL
                    OR btrim(source_group_reference) <> ''
                ),
            ADD CONSTRAINT financial_events_source_group_scope_fk
                FOREIGN KEY (
                    user_id, account_id, source_provider, source_group_reference
                )
                REFERENCES public.financial_source_event_groups (
                    user_id, account_id, source_provider, source_group_reference
                )
                DEFERRABLE INITIALLY DEFERRED
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.ensure_financial_source_event_group()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            IF NEW.source_group_reference IS NOT NULL THEN
                INSERT INTO public.financial_source_event_groups (
                    user_id, account_id, source_provider, source_group_reference
                )
                VALUES (
                    NEW.user_id,
                    NEW.account_id,
                    NEW.source_provider,
                    NEW.source_group_reference
                )
                ON CONFLICT DO NOTHING;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER financial_events_source_group_insert
        BEFORE INSERT OR UPDATE OF
            user_id, account_id, source_provider, source_group_reference
        ON public.financial_events
        FOR EACH ROW EXECUTE FUNCTION public.ensure_financial_source_event_group()
        """
    )

    # Only this documented identity grammar makes a legacy group trustworthy.
    op.execute(
        """
        UPDATE public.financial_events
        SET source_group_reference = regexp_replace(
            source_event_reference,
            ':(base|fee|withholding-tax)$',
            ''
        )
        WHERE source_group_reference IS NULL
            AND source_provider = 'trade-republic'
            AND source_event_reference ~ '^.+:(base|fee|withholding-tax)$'
        """
    )

    op.execute(
        "ALTER TABLE public.financial_source_event_groups ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "REVOKE ALL ON public.financial_source_event_groups "
        "FROM PUBLIC, anon, authenticated"
    )
    op.execute(
        "GRANT SELECT, INSERT ON public.financial_source_event_groups TO authenticated"
    )
    op.execute(
        """
        CREATE POLICY financial_source_event_groups_select_own
        ON public.financial_source_event_groups
        FOR SELECT TO authenticated
        USING ((SELECT auth.uid()) = user_id)
        """
    )
    op.execute(
        """
        CREATE POLICY financial_source_event_groups_insert_own
        ON public.financial_source_event_groups
        FOR INSERT TO authenticated
        WITH CHECK ((SELECT auth.uid()) = user_id)
        """
    )
    _replace_confirmation_function(include_source_group=True)


def downgrade() -> None:
    """Remove group evidence and restore the preceding confirmation contract."""
    _replace_confirmation_function(include_source_group=False)
    op.execute(
        "DROP TRIGGER financial_events_source_group_insert ON public.financial_events"
    )
    op.execute("DROP FUNCTION public.ensure_financial_source_event_group()")
    op.execute(
        "ALTER TABLE public.financial_events "
        "DROP CONSTRAINT financial_events_source_group_scope_fk"
    )
    op.execute("ALTER TABLE public.financial_events DROP COLUMN source_group_reference")
    op.execute("DROP TABLE public.financial_source_event_groups")
