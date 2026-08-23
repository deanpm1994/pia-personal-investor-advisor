"""Persist private provider-neutral EOD market observations.

Revision ID: 20260823_11
Revises: 20260815_08
Create Date: 2026-08-23 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260823_11"
down_revision: str | Sequence[str] | None = "20260815_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create owner-scoped mappings, ingestion evidence, and bar revisions."""
    op.execute(
        """
        CREATE TABLE public.market_provider_access (
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            provider text NOT NULL CHECK (
                provider ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
            ),
            access_status text NOT NULL DEFAULT 'provider_disabled' CHECK (
                access_status IN (
                    'enabled', 'provider_disabled', 'license_review_required'
                )
            ),
            license_checked_at timestamptz,
            license_review_due_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            PRIMARY KEY (user_id, provider),
            CHECK (
                access_status <> 'enabled'
                OR (
                    license_checked_at IS NOT NULL
                    AND license_review_due_at IS NOT NULL
                    AND license_review_due_at > license_checked_at
                    AND license_review_due_at
                        <= license_checked_at + interval '90 days'
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.market_instruments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            isin text NOT NULL CHECK (
                isin ~ '^[A-Z]{2}[A-Z0-9]{9}[0-9]$'
            ),
            share_class_figi text CHECK (
                share_class_figi ~ '^[A-Z0-9]{12}$'
            ),
            instrument_kind text NOT NULL CHECK (
                instrument_kind IN ('common_stock', 'etf')
            ),
            display_name text NOT NULL CHECK (btrim(display_name) <> ''),
            resolution_status text NOT NULL CHECK (
                resolution_status IN (
                    'supported', 'invalid', 'unsupported', 'ambiguous',
                    'temporarily_unavailable', 'provider_disabled'
                )
            ),
            resolution_source_url text NOT NULL CHECK (
                resolution_source_url ~ '^https://'
            ),
            resolved_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            UNIQUE (id, user_id)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX market_instruments_owner_identity_uidx
        ON public.market_instruments (
            user_id, isin, COALESCE(share_class_figi, '')
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.market_provider_identifiers (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL,
            instrument_id uuid NOT NULL,
            provider text NOT NULL CHECK (
                provider ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
            ),
            provider_symbol text NOT NULL CHECK (btrim(provider_symbol) <> ''),
            provider_exchange_code text CHECK (
                provider_exchange_code IS NULL
                OR btrim(provider_exchange_code) <> ''
            ),
            mic text NOT NULL CHECK (mic ~ '^[A-Z0-9]{4}$'),
            quote_currency text NOT NULL CHECK (
                quote_currency ~ '^[A-Z]{3}$'
            ),
            mapping_version integer NOT NULL CHECK (mapping_version > 0),
            valid_from timestamptz NOT NULL,
            valid_to timestamptz,
            resolved_at timestamptz NOT NULL,
            resolution_source_url text NOT NULL CHECK (
                resolution_source_url ~ '^https://'
            ),
            resolution_status text NOT NULL CHECK (
                resolution_status IN (
                    'supported', 'invalid', 'unsupported', 'ambiguous',
                    'temporarily_unavailable', 'provider_disabled'
                )
            ),
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            FOREIGN KEY (instrument_id, user_id)
                REFERENCES public.market_instruments (id, user_id)
                ON DELETE CASCADE,
            UNIQUE (user_id, instrument_id, provider, mapping_version),
            UNIQUE (
                id, user_id, instrument_id, provider, provider_symbol, mic,
                quote_currency, mapping_version
            ),
            CHECK (valid_to IS NULL OR valid_to > valid_from)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX market_provider_identifiers_active_uidx
        ON public.market_provider_identifiers (
            user_id, instrument_id, provider
        ) WHERE valid_to IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE public.market_ingestion_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES public.profiles (id) ON DELETE CASCADE,
            provider text NOT NULL CHECK (
                provider ~ '^[a-z0-9]+(-[a-z0-9]+)*$'
            ),
            status text NOT NULL CHECK (
                status IN (
                    'started', 'completed', 'partial', 'failed',
                    'provider_disabled', 'license_review_required'
                )
            ),
            requested_start date NOT NULL,
            requested_end date NOT NULL,
            provider_as_of timestamptz,
            retrieved_at timestamptz,
            source_url text NOT NULL CHECK (source_url ~ '^https://'),
            input_fingerprint text NOT NULL CHECK (
                input_fingerprint ~ '^[0-9a-f]{64}$'
            ),
            request_parameters jsonb NOT NULL CHECK (
                jsonb_typeof(request_parameters) = 'object'
            ),
            response_sha256 text CHECK (
                response_sha256 ~ '^[0-9a-f]{64}$'
            ),
            completeness_status text NOT NULL CHECK (
                completeness_status IN ('complete', 'incomplete', 'unavailable')
            ),
            diagnostics jsonb NOT NULL CHECK (
                jsonb_typeof(diagnostics) = 'array'
            ),
            quota_state jsonb NOT NULL CHECK (
                jsonb_typeof(quota_state) = 'object'
            ),
            started_at timestamptz NOT NULL,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            UNIQUE (id, user_id, provider),
            CHECK (requested_end >= requested_start),
            CHECK (finished_at IS NULL OR finished_at >= started_at),
            CHECK (
                (status = 'started' AND finished_at IS NULL)
                OR (status <> 'started' AND finished_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.market_eod_bars (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL,
            instrument_id uuid NOT NULL,
            provider_identifier_id uuid NOT NULL,
            ingestion_run_id uuid NOT NULL,
            provider text NOT NULL,
            provider_symbol text NOT NULL,
            mic text NOT NULL,
            quote_currency text NOT NULL,
            mapping_version integer NOT NULL,
            market_date date NOT NULL,
            open numeric(28, 12) NOT NULL CHECK (open > 0),
            high numeric(28, 12) NOT NULL CHECK (high > 0),
            low numeric(28, 12) NOT NULL CHECK (low > 0),
            close numeric(28, 12) NOT NULL CHECK (close > 0),
            volume bigint CHECK (volume IS NULL OR volume >= 0),
            provider_as_of timestamptz NOT NULL,
            retrieved_at timestamptz NOT NULL,
            source_url text NOT NULL CHECK (source_url ~ '^https://'),
            completeness_status text NOT NULL CHECK (
                completeness_status IN ('complete', 'incomplete')
            ),
            revision integer NOT NULL CHECK (revision > 0),
            response_sha256 text NOT NULL CHECK (
                response_sha256 ~ '^[0-9a-f]{64}$'
            ),
            retain_until date NOT NULL,
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            UNIQUE (id, user_id),
            FOREIGN KEY (
                provider_identifier_id, user_id, instrument_id, provider,
                provider_symbol, mic, quote_currency, mapping_version
            ) REFERENCES public.market_provider_identifiers (
                id, user_id, instrument_id, provider, provider_symbol, mic,
                quote_currency, mapping_version
            ) ON DELETE CASCADE,
            FOREIGN KEY (ingestion_run_id, user_id, provider)
                REFERENCES public.market_ingestion_runs (id, user_id, provider)
                ON DELETE CASCADE,
            UNIQUE (
                user_id, provider, provider_symbol, market_date, revision
            ),
            CHECK (high >= low AND high >= open AND high >= close),
            CHECK (low <= open AND low <= close),
            CHECK (
                retain_until >= market_date
                AND retain_until <= market_date + 400
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.market_eod_bar_ingestions (
            user_id uuid NOT NULL,
            bar_id uuid NOT NULL,
            ingestion_run_id uuid NOT NULL,
            provider text NOT NULL,
            provider_as_of timestamptz NOT NULL,
            retrieved_at timestamptz NOT NULL,
            source_url text NOT NULL CHECK (source_url ~ '^https://'),
            response_sha256 text NOT NULL CHECK (
                response_sha256 ~ '^[0-9a-f]{64}$'
            ),
            created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
            PRIMARY KEY (user_id, bar_id, ingestion_run_id),
            FOREIGN KEY (bar_id, user_id)
                REFERENCES public.market_eod_bars (id, user_id) ON DELETE CASCADE,
            FOREIGN KEY (ingestion_run_id, user_id, provider)
                REFERENCES public.market_ingestion_runs (id, user_id, provider)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(
        """
        CREATE INDEX market_eod_bars_owner_listing_date_idx
        ON public.market_eod_bars (
            user_id, instrument_id, market_date DESC, revision DESC
        )
        """
    )
    op.execute(
        """
        CREATE INDEX market_eod_bars_retention_idx
        ON public.market_eod_bars (retain_until, provider)
        """
    )
    op.execute(
        """
        CREATE INDEX market_ingestion_runs_owner_started_idx
        ON public.market_ingestion_runs (user_id, started_at DESC, id DESC)
        """
    )

    for table in (
        "market_provider_access",
        "market_instruments",
        "market_provider_identifiers",
        "market_ingestion_runs",
        "market_eod_bars",
        "market_eod_bar_ingestions",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC, anon, authenticated")
        op.execute(f"GRANT SELECT ON public.{table} TO authenticated")

    for table in (
        "market_provider_access",
        "market_instruments",
        "market_provider_identifiers",
        "market_ingestion_runs",
        "market_eod_bar_ingestions",
    ):
        op.execute(
            f"""
            CREATE POLICY {table}_select_own
            ON public.{table}
            FOR SELECT TO authenticated
            USING ((SELECT auth.uid()) = user_id)
            """
        )
    op.execute(
        """
        CREATE POLICY market_eod_bars_select_licensed_own
        ON public.market_eod_bars
        FOR SELECT TO authenticated
        USING (
            (SELECT auth.uid()) = user_id
            AND retain_until >= current_date
            AND EXISTS (
                SELECT 1
                FROM public.market_provider_access AS access
                WHERE access.user_id = market_eod_bars.user_id
                    AND access.provider = market_eod_bars.provider
                    AND access.access_status = 'enabled'
                    AND access.license_review_due_at > now()
            )
        )
        """
    )


def downgrade() -> None:
    """Remove only the derived market-data store and its access controls."""
    op.execute("DROP TABLE public.market_eod_bar_ingestions")
    op.execute("DROP TABLE public.market_eod_bars")
    op.execute("DROP TABLE public.market_ingestion_runs")
    op.execute("DROP TABLE public.market_provider_identifiers")
    op.execute("DROP TABLE public.market_instruments")
    op.execute("DROP TABLE public.market_provider_access")
