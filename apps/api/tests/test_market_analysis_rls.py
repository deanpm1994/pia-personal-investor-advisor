"""Local-Supabase checks for owner-scoped read-only market analysis."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.services.market_analysis import TrustedMarketAnalysisGateway

pytestmark = pytest.mark.local_supabase

ISIN = "US0000000002"


@pytest.fixture(scope="module")
def database_url() -> str:
    if os.environ.get("PIA_RUN_LOCAL_SUPABASE_TESTS") != "1":
        pytest.skip("set PIA_RUN_LOCAL_SUPABASE_TESTS=1 to run local Supabase tests")
    return Settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _weekdays(count: int, end: date) -> tuple[date, ...]:
    result = []
    candidate = end
    while len(result) < count:
        if candidate.weekday() < 5:
            result.append(candidate)
        candidate -= timedelta(days=1)
    return tuple(reversed(result))


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
        (user_id, f"analysis-{user_id}@example.test"),
    )


def _seed_analysis(connection, owner_id: uuid.UUID) -> None:
    today = datetime.now(UTC).date()
    days = _weekdays(20, today)
    instrument_id = connection.execute(
        """
        INSERT INTO public.market_instruments (
            user_id, isin, share_class_figi, instrument_kind, display_name,
            resolution_status, resolution_source_url, resolved_at
        ) VALUES (
            %s, %s, 'BBG000000001', 'common_stock', 'Synthetic Equity',
            'supported', 'https://mapping.example.test/v3/mapping', now()
        ) RETURNING id
        """,
        (owner_id, ISIN),
    ).fetchone()[0]
    mapping_id = connection.execute(
        """
        INSERT INTO public.market_provider_identifiers (
            user_id, instrument_id, provider, provider_symbol,
            provider_exchange_code, mic, quote_currency, mapping_version,
            valid_from, resolved_at, resolution_source_url, resolution_status
        ) VALUES (
            %s, %s, 'synthetic-eod', 'SYNX.XMAD', 'XMAD', 'XMAD', 'EUR', 1,
            now() - interval '30 days', now() - interval '30 days',
            'https://mapping.example.test/v3/mapping', 'supported'
        ) RETURNING id
        """,
        (owner_id, instrument_id),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO public.market_watchlist_entries (user_id, instrument_id) "
        "VALUES (%s, %s)",
        (owner_id, instrument_id),
    )
    connection.execute(
        """
        INSERT INTO public.market_provider_access (
            user_id, provider, access_status, license_checked_at,
            license_review_due_at
        ) VALUES (
            %s, 'synthetic-eod', 'enabled', now(), now() + interval '30 days'
        )
        """,
        (owner_id,),
    )
    run_id = connection.execute(
        """
        INSERT INTO public.market_ingestion_runs (
            user_id, provider, status, requested_start, requested_end,
            provider_as_of, retrieved_at, source_url, input_fingerprint,
            request_parameters, response_sha256, completeness_status,
            diagnostics, quota_state, started_at, finished_at
        ) VALUES (
            %s, 'synthetic-eod', 'completed', %s, %s, now(), now(),
            'https://data.example.test/v1/eod?symbol=SYNX.XMAD', %s,
            '{"symbol":"SYNX.XMAD"}', %s, 'complete', '[]',
            '{"limit":100,"used":1,"remaining":99}', now(), now()
        ) RETURNING id
        """,
        (owner_id, days[0], days[-1], "b" * 64, "a" * 64),
    ).fetchone()[0]
    for index, market_date in enumerate(days, start=1):
        value = str(index)
        connection.execute(
            """
            INSERT INTO public.market_eod_bars (
                user_id, instrument_id, provider_identifier_id,
                ingestion_run_id, provider, provider_symbol, mic,
                quote_currency, mapping_version, market_date, open, high,
                low, close, volume, provider_as_of, retrieved_at, source_url,
                completeness_status, revision, response_sha256, retain_until
            ) VALUES (
                %s, %s, %s, %s, 'synthetic-eod', 'SYNX.XMAD', 'XMAD',
                'EUR', 1, %s, %s, %s, %s, %s, 1000, now(), now(),
                'https://data.example.test/v1/eod?symbol=SYNX.XMAD',
                'complete', 1, %s, %s
            )
            """,
            (
                owner_id,
                instrument_id,
                mapping_id,
                run_id,
                market_date,
                value,
                value,
                value,
                value,
                "a" * 64,
                market_date + timedelta(days=30),
            ),
        )
    content = {
        "positions": {
            "owner": [
                {
                    "instrument_id": ISIN,
                    "quantity": "2.000000000000",
                    "evidence_event_ids": ["buy-1"],
                }
            ]
        },
        "fifo": {
            "open_lots": [
                {
                    "instrument_id": ISIN,
                    "quantity": "2.000000000000",
                    "total_basis": "20.000000000000",
                    "source_currency": "EUR",
                    "evidence_event_ids": ["buy-1"],
                }
            ]
        },
    }
    connection.execute(
        """
        INSERT INTO public.financial_snapshots (
            user_id, input_fingerprint, input_watermark, input_counts, content
        ) VALUES (%s, %s, '{}', '{}', %s)
        """,
        (owner_id, "c" * 64, Jsonb(content)),
    )


def test_gateway_reads_only_owner_content_and_honors_provider_disablement(
    database_url: str,
) -> None:
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    gateway = TrustedMarketAnalysisGateway(Settings(database_url=database_url))
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _insert_auth_user(connection, owner_id)
            _insert_auth_user(connection, other_id)
            _seed_analysis(connection, owner_id)

        owner_items = asyncio.run(
            gateway.list_analysis(AuthenticatedUser(id=str(owner_id), email=None))
        )
        other_items = asyncio.run(
            gateway.list_analysis(AuthenticatedUser(id=str(other_id), email=None))
        )

        assert len(owner_items) == 1
        assert owner_items[0]["source_kind"] == "portfolio_and_watchlist"
        assert owner_items[0]["valuation"]["current_value"] == "40.000000000000"
        assert other_items == []

        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE public.market_provider_access
                SET access_status = 'provider_disabled'
                WHERE user_id = %s AND provider = 'synthetic-eod'
                """,
                (owner_id,),
            )
        disabled = asyncio.run(
            gateway.list_analysis(AuthenticatedUser(id=str(owner_id), email=None))
        )
        assert disabled[0]["state"] == "provider_disabled"
        assert disabled[0]["bars"] == []
        assert disabled[0]["valuation"] is None
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)",
                (owner_id, other_id),
            )
