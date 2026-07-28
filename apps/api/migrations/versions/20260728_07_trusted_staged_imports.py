"""Restrict staged-import writes to the trusted server workflow.

Revision ID: 20260728_07
Revises: 20260723_06
Create Date: 2026-07-28 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260728_07"
down_revision: str | Sequence[str] | None = "20260723_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Require server-persisted parser output before confirmation."""
    op.execute(
        """
        ALTER TABLE public.staged_imports
            ADD COLUMN trusted_staged_at timestamptz
        """
    )
    op.execute(
        """
        ALTER TABLE public.staged_imports
            ADD CONSTRAINT staged_imports_trusted_staged_at_after_created_check
            CHECK (trusted_staged_at IS NULL OR trusted_staged_at >= created_at)
        """
    )
    for table in (
        "staged_imports",
        "staged_import_files",
        "staged_import_rows",
        "staged_import_validation_results",
        "staged_import_state_events",
    ):
        op.execute(f"REVOKE INSERT ON public.{table} FROM authenticated")

    op.execute(
        """
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
                        source_reported_eur_reported_at
                    )
                    VALUES (
                        v_user_id,
                        v_account_id,
                        p_staged_import_id,
                        v_candidate #>> '{source_identity,provider}',
                        v_candidate #>> '{source_identity,event_reference}',
                        v_candidate ->> 'event_type',
                        (v_candidate ->> 'occurred_at')::timestamptz,
                        NULLIF(
                            v_candidate #>> '{source_reported_eur,eur_amount,amount}',
                            ''
                        )::numeric,
                        NULLIF(
                            v_candidate #>> '{source_reported_eur,source_rate}', ''
                        )::numeric,
                        NULLIF(
                            v_candidate #>> '{source_reported_eur,reported_at}',
                            ''
                        )::timestamptz
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
                            THEN (value #>> '{money,amount}')::numeric END,
                        CASE WHEN value ->> 'kind' = 'cash'
                            THEN value #>> '{money,currency}' END,
                        CASE WHEN value ->> 'kind' = 'instrument'
                            THEN value ->> 'instrument_id' END,
                        CASE WHEN value ->> 'kind' = 'instrument'
                            THEN (value #>> '{quantity,value}')::numeric END
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


def downgrade() -> None:
    """Restore the P4.2 client append boundary."""
    op.execute(
        """
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

            PERFORM 1
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

            IF v_state <> 'review_ready' THEN
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
                        source_reported_eur_reported_at
                    )
                    VALUES (
                        v_user_id, v_account_id, p_staged_import_id,
                        v_candidate #>> '{source_identity,provider}',
                        v_candidate #>> '{source_identity,event_reference}',
                        v_candidate ->> 'event_type',
                        (v_candidate ->> 'occurred_at')::timestamptz,
                        NULLIF(
                            v_candidate #>> '{source_reported_eur,eur_amount,amount}',
                            ''
                        )::numeric,
                        NULLIF(
                            v_candidate #>> '{source_reported_eur,source_rate}', ''
                        )::numeric,
                        NULLIF(
                            v_candidate #>> '{source_reported_eur,reported_at}', ''
                        )::timestamptz
                    ) RETURNING id INTO v_event_id;
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
                        v_event_id, v_user_id, v_account_id, ordinality::integer,
                        value ->> 'kind', value ->> 'direction',
                        CASE WHEN value ->> 'kind'
                            = 'cash' THEN (value #>> '{money,amount}')::numeric END,
                        CASE WHEN value ->> 'kind'
                            = 'cash' THEN value #>> '{money,currency}' END,
                        CASE WHEN value ->> 'kind'
                            = 'instrument' THEN value ->> 'instrument_id' END,
                        CASE WHEN value ->> 'kind'
                            = 'instrument'
                            THEN (value #>> '{quantity,value}')::numeric END
                    FROM jsonb_array_elements(v_candidate -> 'legs') WITH ORDINALITY;
                    v_event_count := v_event_count + 1;
                END LOOP;
            END LOOP;
            SELECT coalesce(max(position), 0) + 1 INTO v_next_position
            FROM public.staged_import_state_events
            WHERE staged_import_id = p_staged_import_id;
            INSERT INTO public.staged_import_state_events (
                user_id, staged_import_id, position, state, details
            ) VALUES (
                v_user_id, p_staged_import_id, v_next_position, 'confirmed',
                jsonb_build_object('event_count', v_event_count)
            );
            INSERT INTO public.audit_events (actor_id, event_type, metadata)
            VALUES (v_user_id, 'import.confirmed', jsonb_build_object(
                'staged_import_id', p_staged_import_id, 'event_count', v_event_count
            ));
            RETURN QUERY SELECT v_event_count, false;
        END;
        $$
        """
    )
    for table in (
        "staged_imports",
        "staged_import_files",
        "staged_import_rows",
        "staged_import_validation_results",
        "staged_import_state_events",
    ):
        op.execute(f"GRANT INSERT ON public.{table} TO authenticated")
    op.execute(
        "ALTER TABLE public.staged_imports "
        "DROP CONSTRAINT staged_imports_trusted_staged_at_after_created_check"
    )
    op.execute("ALTER TABLE public.staged_imports DROP COLUMN trusted_staged_at")
