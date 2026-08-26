"""Local-Supabase checks for Marketstack gates, quota, and schedule audit RLS."""

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.services.market_ingestion import (
    ATTESTATION_VERSION,
    MarketEodJobResult,
    TrustedMarketIngestionStore,
)

pytestmark = pytest.mark.local_supabase


@pytest.fixture(scope="module")
def database_url() -> str:
    if os.environ.get("PIA_RUN_LOCAL_SUPABASE_TESTS") != "1":
        pytest.skip("set PIA_RUN_LOCAL_SUPABASE_TESTS=1 to run local Supabase tests")
    return Settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _insert_auth_user(connection, user_id: uuid.UUID) -> None:
    connection.execute(
        """
        INSERT INTO auth.users (
            id, instance_id, aud, role, email, encrypted_password,
            email_confirmed_at, raw_app_meta_data, raw_user_meta_data,
            created_at, updated_at
        ) VALUES (
            %s, '00000000-0000-0000-0000-000000000000', 'authenticated',
            'authenticated', %s, '', now(),
            '{"provider":"email","providers":["email"]}', '{}', now(), now()
        )
        """,
        (user_id, f"schedule-{user_id}@example.test"),
    )


def _as_authenticated_user(connection, user_id: uuid.UUID) -> None:
    connection.execute("SET LOCAL ROLE authenticated")
    connection.execute(
        "SELECT set_config('request.jwt.claim.sub', %s, true)", (str(user_id),)
    )
    connection.execute(
        "SELECT set_config('request.jwt.claim.role', 'authenticated', true)"
    )


def test_marketstack_attestation_quota_and_schedule_audit_are_fail_closed(
    database_url: str,
) -> None:
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    user = AuthenticatedUser(id=str(owner_id), email=None)
    store = TrustedMarketIngestionStore(Settings(database_url=database_url))
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _insert_auth_user(connection, owner_id)
            _insert_auth_user(connection, other_id)
            with pytest.raises(psycopg.errors.CheckViolation):
                connection.execute(
                    """
                    INSERT INTO public.market_provider_access (
                        user_id, provider, access_status, license_checked_at,
                        license_review_due_at
                    ) VALUES (
                        %s, 'marketstack', 'enabled', now(),
                        now() + interval '1 day'
                    )
                    """,
                    (owner_id,),
                )
            connection.execute(
                """
                INSERT INTO public.market_provider_access (
                    user_id, provider, access_status, license_checked_at,
                    license_review_due_at, risk_attestation_version, risk_attested_at
                ) VALUES (
                    %s, 'marketstack', 'enabled', now(), now() + interval '89 days',
                    %s, now()
                )
                """,
                (owner_id, ATTESTATION_VERSION),
            )

        run_at = datetime.now(UTC)
        assert asyncio.run(store.provider_gate(user, run_at)).allowed
        with psycopg.connect(database_url, autocommit=True) as connection:
            instrument_id = connection.execute(
                """
                INSERT INTO public.market_instruments (
                    user_id, isin, instrument_kind, display_name,
                    resolution_status, resolution_source_url, resolved_at
                ) VALUES (
                    %s, 'US0378331005', 'common_stock', 'Synthetic Equity',
                    'supported', 'https://mapping.example.test/marketstack', now()
                ) RETURNING id
                """,
                (owner_id,),
            ).fetchone()[0]
            mapping_id = connection.execute(
                """
                INSERT INTO public.market_provider_identifiers (
                    user_id, instrument_id, provider, provider_symbol,
                    provider_exchange_code, mic, quote_currency, mapping_version,
                    valid_from, resolved_at, resolution_source_url, resolution_status
                ) VALUES (
                    %s, %s, 'marketstack', 'SYNX', 'XMAD', 'XMAD', 'EUR', 1,
                    now(), now(), 'https://mapping.example.test/marketstack',
                    'supported'
                ) RETURNING id
                """,
                (owner_id, instrument_id),
            ).fetchone()[0]
            ingestion_id = connection.execute(
                """
                INSERT INTO public.market_ingestion_runs (
                    user_id, provider, status, requested_start, requested_end,
                    source_url, input_fingerprint, request_parameters,
                    response_sha256, completeness_status, diagnostics, quota_state,
                    started_at, finished_at, provider_as_of, retrieved_at
                ) VALUES (
                    %s, 'marketstack', 'completed', %s, %s,
                    'https://api.marketstack.com/v2/eod?symbols=SYNX', %s,
                    '{"symbols":"SYNX"}', %s, 'complete', '[]',
                    '{"limit":100,"used":1,"remaining":99}',
                    now(), now(), now(), now()
                ) RETURNING id
                """,
                (owner_id, run_at.date(), run_at.date(), "b" * 64, "a" * 64),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO public.market_eod_bars (
                    user_id, instrument_id, provider_identifier_id,
                    ingestion_run_id, provider, provider_symbol, mic,
                    quote_currency, mapping_version, market_date, open, high,
                    low, close, volume, provider_as_of, retrieved_at, source_url,
                    completeness_status, revision, response_sha256, retain_until
                ) VALUES (
                    %s, %s, %s, %s, 'marketstack', 'SYNX', 'XMAD', 'EUR', 1,
                    %s, 10.10, 11.20, 9.90, 11.00, 1200, now(), now(),
                    'https://api.marketstack.com/v2/eod?symbols=SYNX', 'complete',
                    1, %s, %s
                )
                """,
                (
                    owner_id,
                    instrument_id,
                    mapping_id,
                    ingestion_id,
                    run_at.date(),
                    "a" * 64,
                    run_at.date() + timedelta(days=30),
                ),
            )
        assert (
            asyncio.run(store.purge_content(user, run_at, all_provider_content=True))
            == 1
        )
        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                "SELECT count(*) FROM market_eod_bars WHERE user_id = %s",
                (owner_id,),
            ).fetchone() == (0,)
        for _ in range(72):
            quota = asyncio.run(store.reserve_quota(user, 1, run_at))
            assert quota is not None
        assert asyncio.run(store.reserve_quota(user, 1, run_at)) is None
        for _ in range(28):
            quota = asyncio.run(store.reserve_quota(user, 2, run_at))
            assert quota is not None
        assert quota.used == 100
        assert asyncio.run(store.reserve_quota(user, 2, run_at)) is None

        result = MarketEodJobResult(
            str(uuid.uuid4()), "completed", date.today(), 0, 0, 0, ()
        )
        asyncio.run(
            store.record_job(
                user,
                uuid.UUID(result.job_id),
                run_at,
                result,
                run_at,
                run_at + timedelta(seconds=1),
            )
        )

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, owner_id)
                assert connection.execute(
                    "SELECT routine_requests, reserve_requests FROM market_quota_usage"
                ).fetchone() == (72, 28)
                assert connection.execute(
                    "SELECT status FROM market_schedule_runs"
                ).fetchone() == ("completed",)
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with connection.transaction():
                        connection.execute(
                            "UPDATE market_quota_usage SET routine_requests = 0"
                        )
            with connection.transaction():
                _as_authenticated_user(connection, other_id)
                assert (
                    connection.execute("SELECT * FROM market_quota_usage").fetchall()
                    == []
                )
                assert (
                    connection.execute("SELECT * FROM market_schedule_runs").fetchall()
                    == []
                )

        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE public.market_provider_access
                SET risk_withdrawn_at = now(), access_status = 'provider_disabled'
                WHERE user_id = %s AND provider = 'marketstack'
                """,
                (owner_id,),
            )
        withdrawn_gate = asyncio.run(
            store.provider_gate(user, run_at + timedelta(seconds=1))
        )
        assert not withdrawn_gate.allowed
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)", (owner_id, other_id)
            )
