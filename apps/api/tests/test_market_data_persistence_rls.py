"""Local-Supabase checks for private, revision-preserving market storage."""

import asyncio
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from pia_api.core.auth import AuthenticatedUser
from pia_api.core.config import Settings
from pia_api.domain.market_data import (
    CompletenessStatus,
    DailyBar,
    FetchOutcome,
    ProviderMapping,
    QuotaState,
    ResolutionStatus,
    assess_fetch_outcome,
)
from pia_api.services.market_data import (
    MarketDataAccessError,
    TrustedMarketDataGateway,
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
        (user_id, f"market-{user_id}@example.test"),
    )


def _as_authenticated_user(connection, user_id: uuid.UUID) -> None:
    connection.execute("SET LOCAL ROLE authenticated")
    connection.execute(
        "SELECT set_config('request.jwt.claim.sub', %s, true)", (str(user_id),)
    )
    connection.execute(
        "SELECT set_config('request.jwt.claim.role', 'authenticated', true)"
    )


def _seed_market_data(
    connection, user_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    market_date = date.today() - timedelta(days=1)
    retain_until = date.today() + timedelta(days=30)
    instrument_id = connection.execute(
        """
        INSERT INTO public.market_instruments (
            user_id, isin, share_class_figi, instrument_kind, display_name,
            resolution_status, resolution_source_url, resolved_at
        ) VALUES (
            %s, 'US0000000002', 'BBG000000001', 'common_stock',
            'Synthetic Equity', 'supported',
            'https://mapping.example.test/v3/mapping', now()
        ) RETURNING id
        """,
        (user_id,),
    ).fetchone()[0]
    mapping_id = connection.execute(
        """
        INSERT INTO public.market_provider_identifiers (
            user_id, instrument_id, provider, provider_symbol,
            provider_exchange_code, mic, quote_currency, mapping_version,
            valid_from, resolved_at, resolution_source_url, resolution_status
        ) VALUES (
            %s, %s, 'synthetic-eod', 'SYNX.XMAD', 'XMAD', 'XMAD', 'EUR', 1,
            '2026-08-01T00:00:00Z', '2026-08-01T12:00:00Z',
            'https://mapping.example.test/v3/mapping', 'supported'
        ) RETURNING id
        """,
        (user_id, instrument_id),
    ).fetchone()[0]
    run_id = connection.execute(
        """
        INSERT INTO public.market_ingestion_runs (
            user_id, provider, status, requested_start, requested_end,
            source_url, request_parameters, response_sha256,
            input_fingerprint, completeness_status, diagnostics, quota_state,
            started_at,
            finished_at, provider_as_of, retrieved_at
        ) VALUES (
            %s, 'synthetic-eod', 'completed', '2026-08-21', '2026-08-21',
            'https://data.example.test/v1/eod?symbol=SYNX.XMAD',
            '{"symbol":"SYNX.XMAD"}', %s, %s, 'complete', '[]',
            '{"limit":100,"used":1,"remaining":99}', now(), now(),
            '2026-08-21T23:00:00Z', '2026-08-22T06:00:00Z'
        ) RETURNING id
        """,
        (user_id, "a" * 64, "c" * 64),
    ).fetchone()[0]
    bar_id = connection.execute(
        """
        INSERT INTO public.market_eod_bars (
            user_id, instrument_id, provider_identifier_id, ingestion_run_id,
            provider, provider_symbol, mic, quote_currency, mapping_version,
            market_date, open, high, low, close, volume, provider_as_of,
            retrieved_at, source_url, completeness_status, revision,
            response_sha256, retain_until
        ) VALUES (
            %s, %s, %s, %s, 'synthetic-eod', 'SYNX.XMAD', 'XMAD', 'EUR', 1,
            %s, 10.10, 11.20, 9.90, 11.00, 1200,
            '2026-08-21T23:00:00Z', '2026-08-22T06:00:00Z',
            'https://data.example.test/v1/eod?symbol=SYNX.XMAD', 'complete', 1,
            %s, %s
        ) RETURNING id
        """,
        (
            user_id,
            instrument_id,
            mapping_id,
            run_id,
            market_date,
            "a" * 64,
            retain_until,
        ),
    ).fetchone()[0]
    return instrument_id, mapping_id, bar_id


def test_market_storage_is_owner_scoped_decimal_safe_and_license_gated(
    database_url: str,
) -> None:
    owner_id, other_id = uuid.uuid4(), uuid.uuid4()
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _insert_auth_user(connection, owner_id)
            _insert_auth_user(connection, other_id)
            instrument_id, _, bar_id = _seed_market_data(connection, owner_id)

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, owner_id)
                assert connection.execute(
                    "SELECT id FROM public.market_instruments"
                ).fetchall() == [(instrument_id,)]
                assert (
                    connection.execute(
                        "SELECT id FROM public.market_eod_bars"
                    ).fetchall()
                    == []
                )

        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                """
                INSERT INTO public.market_provider_access (
                    user_id, provider, access_status, license_checked_at,
                    license_review_due_at
                ) VALUES (
                    %s, 'synthetic-eod', 'enabled', now(),
                    now() + interval '90 days'
                )
                """,
                (owner_id,),
            )

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, owner_id)
                row = connection.execute(
                    """
                    SELECT id, open::text, high::text, low::text, close::text
                    FROM public.market_eod_bars
                    """
                ).fetchone()
                assert row == (
                    bar_id,
                    "10.100000000000",
                    "11.200000000000",
                    "9.900000000000",
                    "11.000000000000",
                )
                for statement in (
                    "UPDATE public.market_eod_bars SET close = 12.00",
                    "DELETE FROM public.market_eod_bars",
                    "INSERT INTO public.market_provider_access (user_id, provider) "
                    "VALUES (auth.uid(), 'forbidden')",
                ):
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with connection.transaction():
                            connection.execute(statement)

        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, other_id)
                assert (
                    connection.execute(
                        "SELECT id FROM public.market_instruments"
                    ).fetchall()
                    == []
                )
                assert (
                    connection.execute(
                        "SELECT id FROM public.market_eod_bars"
                    ).fetchall()
                    == []
                )

        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE public.market_eod_bars
                SET retain_until = market_date
                WHERE user_id = %s
                """,
                (owner_id,),
            )
        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, owner_id)
                assert (
                    connection.execute(
                        "SELECT id FROM public.market_eod_bars"
                    ).fetchall()
                    == []
                )

        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                """
                UPDATE public.market_provider_access
                SET access_status = 'provider_disabled'
                WHERE user_id = %s AND provider = 'synthetic-eod'
                """,
                (owner_id,),
            )
        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                _as_authenticated_user(connection, owner_id)
                assert (
                    connection.execute(
                        "SELECT id FROM public.market_eod_bars"
                    ).fetchall()
                    == []
                )
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM auth.users WHERE id IN (%s, %s)", (owner_id, other_id)
            )


def test_market_storage_revisions_and_constraints_are_deterministic(
    database_url: str,
) -> None:
    owner_id = uuid.uuid4()
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _insert_auth_user(connection, owner_id)
            _, _, bar_id = _seed_market_data(connection, owner_id)
            baseline = connection.execute(
                "SELECT * FROM public.market_eod_bars WHERE id = %s", (bar_id,)
            ).fetchone()
            columns = [
                column.name
                for column in connection.execute(
                    "SELECT * FROM public.market_eod_bars WHERE id = %s", (bar_id,)
                ).description
            ]
            replay = dict(zip(columns, baseline, strict=True))
            replay.pop("id")
            replay.pop("created_at")

            column_names = ", ".join(replay)
            placeholders = ", ".join(["%s"] * len(replay))
            with pytest.raises(psycopg.errors.UniqueViolation):
                connection.execute(
                    f"INSERT INTO public.market_eod_bars ({column_names}) "
                    f"VALUES ({placeholders})",
                    tuple(replay.values()),
                )

            replay["revision"] = 2
            replay["close"] = "10.50"
            replay["response_sha256"] = "b" * 64
            correction_id = connection.execute(
                f"INSERT INTO public.market_eod_bars ({column_names}) "
                f"VALUES ({placeholders}) RETURNING id",
                tuple(replay.values()),
            ).fetchone()[0]
            assert correction_id != bar_id

            replay["revision"] = 3
            replay["retain_until"] = replay["market_date"] + timedelta(days=401)
            with pytest.raises(psycopg.errors.CheckViolation):
                connection.execute(
                    f"INSERT INTO public.market_eod_bars ({column_names}) "
                    f"VALUES ({placeholders})",
                    tuple(replay.values()),
                )
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute("DELETE FROM auth.users WHERE id = %s", (owner_id,))


def _fetch_contracts(
    instrument_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    close: str = "11.00",
    response_sha256: str = "d" * 64,
) -> tuple[ProviderMapping, FetchOutcome]:
    market_date = date.today() - timedelta(days=1)
    retrieved_at = datetime.now(UTC)
    mapping = ProviderMapping(
        instrument_id=instrument_id,
        provider="synthetic-eod",
        provider_symbol="SYNX.XMAD",
        provider_exchange_code="XMAD",
        mic="XMAD",
        quote_currency="EUR",
        mapping_version=1,
        valid_from=datetime(2026, 8, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
        resolution_source_url="https://mapping.example.test/v3/mapping",
        resolution_status=ResolutionStatus.SUPPORTED,
    )
    bar = DailyBar(
        listing_id=instrument_id,
        market_date=market_date,
        open="10.10",
        high="11.20",
        low="9.90",
        close=close,
        volume=1200,
        provider="synthetic-eod",
        provider_symbol="SYNX.XMAD",
        mic="XMAD",
        quote_currency="EUR",
        provider_as_of=retrieved_at - timedelta(hours=7),
        retrieved_at=retrieved_at,
        ingestion_run_id=run_id,
        source_url="https://data.example.test/v1/eod?symbol=SYNX.XMAD",
        mapping_version=1,
        completeness_status=CompletenessStatus.COMPLETE,
        revision=1,
        response_sha256=response_sha256,
    )
    outcome = FetchOutcome(
        ingestion_run_id=run_id,
        provider="synthetic-eod",
        provider_symbol="SYNX.XMAD",
        mic="XMAD",
        quote_currency="EUR",
        requested_start=market_date,
        requested_end=market_date,
        provider_as_of=bar.provider_as_of,
        started_at=retrieved_at - timedelta(minutes=1),
        retrieved_at=retrieved_at,
        source_url=bar.source_url,
        request_parameters={"symbol": "SYNX.XMAD"},
        response_sha256=response_sha256,
        completeness_status=CompletenessStatus.COMPLETE,
        quota_state=QuotaState(limit=100, used=1, remaining=99),
        bars=(bar,),
    )
    return mapping, outcome


def test_trusted_persistence_reuses_identical_bars_and_appends_corrections(
    database_url: str,
) -> None:
    owner_id = uuid.uuid4()
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            _insert_auth_user(connection, owner_id)
            instrument_id, mapping_id, baseline_bar_id = _seed_market_data(
                connection, owner_id
            )

        gateway = TrustedMarketDataGateway(Settings(database_url=database_url))
        user = AuthenticatedUser(id=str(owner_id), email=None)
        mapping, replay = _fetch_contracts(instrument_id, uuid.uuid4())
        replay_assessment = assess_fetch_outcome(
            replay, mapping=mapping, target_date=replay.requested_end
        )

        with pytest.raises(MarketDataAccessError):
            asyncio.run(
                gateway.persist_fetch(
                    user, mapping_id, mapping, replay, replay_assessment
                )
            )

        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(
                """
                INSERT INTO public.market_provider_access (
                    user_id, provider, access_status, license_checked_at,
                    license_review_due_at
                ) VALUES (
                    %s, 'synthetic-eod', 'enabled', now(), now() + interval '90 days'
                )
                """,
                (owner_id,),
            )

        first = asyncio.run(
            gateway.persist_fetch(user, mapping_id, mapping, replay, replay_assessment)
        )
        second = asyncio.run(
            gateway.persist_fetch(user, mapping_id, mapping, replay, replay_assessment)
        )
        assert first == second
        assert first.bar_ids == (str(baseline_bar_id),)
        assert first.inserted_revisions == 0
        assert first.reused_bars == 1

        _, correction = _fetch_contracts(
            instrument_id,
            uuid.uuid4(),
            close="10.50",
            response_sha256="e" * 64,
        )
        correction_assessment = assess_fetch_outcome(
            correction, mapping=mapping, target_date=correction.requested_end
        )
        corrected = asyncio.run(
            gateway.persist_fetch(
                user, mapping_id, mapping, correction, correction_assessment
            )
        )
        assert corrected.inserted_revisions == 1
        assert corrected.reused_bars == 0

        with psycopg.connect(database_url) as connection:
            assert connection.execute(
                """
                SELECT revision, close::text, response_sha256
                FROM public.market_eod_bars
                WHERE user_id = %s
                ORDER BY revision
                """,
                (owner_id,),
            ).fetchall() == [
                (1, "11.000000000000", "a" * 64),
                (2, "10.500000000000", "e" * 64),
            ]
            assert connection.execute(
                """
                SELECT count(*) FROM public.market_eod_bar_ingestions
                WHERE user_id = %s
                """,
                (owner_id,),
            ).fetchone() == (2,)
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute("DELETE FROM auth.users WHERE id = %s", (owner_id,))


def test_market_data_migration_downgrades_and_upgrades(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    command.downgrade(config, "20260815_08")
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT to_regclass('public.market_eod_bars')"
        ).fetchone() == (None,)
    command.upgrade(config, "head")
    with psycopg.connect(database_url) as connection:
        assert connection.execute(
            "SELECT to_regclass('public.market_eod_bars')"
        ).fetchone() == ("market_eod_bars",)
